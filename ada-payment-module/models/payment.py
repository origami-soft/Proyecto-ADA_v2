# coding: utf-8
import logging
import json

from werkzeug import urls

import logging

from odoo import _, api, fields, models
from odoo.addons.payment.models.payment_acquirer import ValidationError
from odoo.tools.float_utils import float_compare
from ..connectors.currency_conversion import COINMARKET_PROVIDER
from ..connectors.conversion_providers.coinmarket import CoinMarket
from ..connectors.currency_conversion import get_conversion_provider
from .utils import generate_b64_qr_image
from ..connectors.adapay import get_adapay_api
from datetime import datetime, timedelta

_logger = logging.getLogger(__name__)

ADA = "ADA"


class AcquirerAdapay(models.Model):
    _inherit = 'payment.acquirer'

    provider = fields.Selection(selection_add=[
        ('adapay', 'Adapay')
    ], ondelete={'adapay': 'set default'})
    adapay_api_key = fields.Char("AdaPay APIKEY")
    adapay_expiration_minutes = fields.Integer("Payment request expiration (minutes)", default=15)
    conversion_provider = fields.Selection([
            (COINMARKET_PROVIDER, 'CoinMarket')
        ],
        default=COINMARKET_PROVIDER, required=True)
    conversion_api_key = fields.Char("Api-Key")
    conversion_test_mode = fields.Boolean("Test Mode", default=False)

    def _get_conversion_provider(self, data):
        return get_conversion_provider(**data)

    def adapay_form_generate_values(self, values):
        dict(values)
        # Perform currency amount conversion
        try:
            currency_converter = self._get_conversion_provider(
                {"provider": self.conversion_provider, "api_key": self.conversion_api_key, "sandbox": self.conversion_test_mode })
            resp = currency_converter.price_conversion(
                amount=values["amount"],
                amount_currency=values["currency"].name,
                convert_currency=ADA,
            )
            adapay_amount = {
                "ada_amount": int(resp["price"]),
                "last_updated": resp["last_updated"],
                "ada_currency": ADA,
            }
            _logger.info(f"{self.conversion_provider}: {values['amount']} {values['currency'].name} -> {resp['price']} {ADA}. Last updated: {resp['last_updated']}")
            values.update(adapay_amount)
        except Exception as err:
            raise ValidationError("We cannot get the ADA currency rate. Please try again.")
        # Perform adapay payment requests
        try:
            sandbox = self.state == "test"
            adapay_conn = get_adapay_api(self.adapay_api_key, sandbox)
            payment_uuid = adapay_conn.create_payment(
                amount=values["ada_amount"]*1000000,  #TODO: handle in connector
                expiration_minutes=self.adapay_expiration_minutes,
                receipt_email=values["partner_email"],
                description=values["reference"],
                name=values["reference"],
                order_id=values["reference"],
            )
            payment_data = adapay_conn.get_payment_by_uuid(payment_uuid["uuid"])
            # Add `adapay` suffix to data
            payment_data = {f"adapay_{key}":value for key, value in payment_data.items()}
            values.update(payment_data)
            values["adapay_expiration_minutes"] = self.adapay_expiration_minutes
        except Exception:
            raise ValidationError("We cannot create the AdaPay payment request. Please try again.")
        return values

    def adapay_get_form_action_url(self):
        self.ensure_one()
        return '/payment/adapay/accept'


class TransactionAdapay(models.Model):
    _inherit = 'payment.transaction'

    adapay_uuid = fields.Char("Payment UUID")
    adapay_amount = fields.Integer("Amount in ADA")
    adapay_total_amount_sent = fields.Integer("Amount sent")
    adapay_total_amount_left = fields.Integer("Amount left")
    adapay_expiration_minutes = fields.Integer("Expiration minutes")
    adapay_expiration_date = fields.Char("Expiration date")
    adapay_update_time = fields.Char("Update time")
    adapay_last_updated= fields.Char("Last updated")
    adapay_status = fields.Char("Payment status", default="new")
    adapay_address = fields.Char("Payment address")
    adapay_transaction_history = fields.Text("Transaction history", default='{}')

    @api.model
    def _adapay_form_get_tx_from_data(self, data, field="reference"):
        reference = data.get("reference")
        tx = self.search([(field, '=', reference)])
        if not tx or len(tx) > 1:
            error_msg = _('received data for reference %s') % (reference)
            if not tx:
                error_msg += _('; no order found')
            else:
                error_msg += _('; multiple order found')
            _logger.info(error_msg)
            raise ValidationError(error_msg)
        return tx

    def _get_tx_from_uuid(self, uuid):
        tx = self.search([('adapay_uuid', '=', uuid)])
        if not tx or len(tx) > 1:
            error_msg = _('received data for uuid %s') % (uuid)
            if not tx:
                error_msg += _('; no order found')
            else:
                error_msg += _('; multiple order found')
            _logger.info(error_msg)
            raise ValidationError(error_msg)
        return tx

    def adapay_create(self, data):
        return data

    def _adapay_form_get_invalid_parameters(self, data):
        invalid_parameters = []
        # TODO: restore validation
        # if float_compare(float(data.get('amount') or '0.0'), self.amount, 2) != 0:
        #    invalid_parameters.append(('amount', data.get('amount'), '%.2f' % self.amount))
        if data.get('currency') != self.currency_id.name:
            invalid_parameters.append(('currency', data.get('currency'), self.currency_id.name))
        return invalid_parameters

    def _adapay_form_validate(self, data):
        _logger.info('Validated adapay payment for tx %s: set as pending' % (self.reference))
        data["adapay_amount"] = int(data["adapay_amount"]) / 1000000
        # Update tx `adapay_` fields
        adapay_data = {key: value for key, value in data.items() if key.startswith("adapay_")}
        self.write(adapay_data)
        for sale in self.sale_order_ids:
            sale.message_post(body=f"AdaPay payment request created with data: {adapay_data}")
        self._set_transaction_pending()
        return True

    def _get_processing_info(self):
        qr_encoded = generate_b64_qr_image(self.adapay_address)
        res = super()._get_processing_info()
        title = {
            "new": "To complete your payment, please send ADA to the address below.",
            "payment-sent": "Your payment was received! You payment is pending for confirmation.",
            "pending": "The transaction is in the process of being confirmed.",
            "confirmed": "The transaction is confirmed!",
            "expired": "The payment request is expired. Please back to payment selection."
        }
        if self.acquirer_id.provider == 'adapay':
            # Render AdaPay payment pages
            now = datetime.now()
            expiration = self.create_date + timedelta(minutes=self.adapay_expiration_minutes)
            if now > expiration:
                self.adapay_status = "expired"
                exp_time = 0
            else: 
                exp_time = (expiration - now).seconds
            transaction_adapay_data = {field:getattr(self, field) for field in self._fields.keys() if field.startswith("adapay")}
            transaction_adapay_data['qr_code'] = f"data:image/png;base64,{qr_encoded}"
            transaction_adapay_data['adapay_transaction_history'] = list(json.loads(self.adapay_transaction_history).values())
            transaction_adapay_data['title'] = title[self.adapay_status]
            payment_frame = self.env.ref("ada-payment-module.payment_adapay_page")._render(transaction_adapay_data, engine="ir.qweb")
            adapay_info = {
                "message_to_display": payment_frame,
                "adapay_expiration_seconds": exp_time,
            }
            if self.adapay_status == "new":  # Prevent confirmation page if payment was not started
                adapay_info["return_url"] = ""
            res.update(adapay_info)
        return res

    def _handle_adapay_webhook(self, data):
        """"""
        event_type = data.get("event")
        data = data.get("data", {})
        if event_type == 'paymentRequestUpdate':
            tx_reference = data.get("orderId")
            data_tx = {'reference': tx_reference}
            try:
                odoo_tx = self.env['payment.transaction']._adapay_form_get_tx_from_data(data_tx)
            except ValidationError as e:
                _logger.error('Received notification for tx %s: %s', tx_reference, e)
                return False
            odoo_tx._adapay_webhook_feedback(data)
        elif event_type == 'paymentRequestTransactionsUpdate':
            tx_reference = data.get("paymentRequestUuid")
            data_tx = {'reference': tx_reference}
            try:
                odoo_tx = self.env['payment.transaction']._adapay_form_get_tx_from_data(data_tx, field="adapay_uuid")
            except ValidationError as e:
                _logger.error('Received notification for tx %s: %s', tx_reference, e)
                return False
            odoo_tx._adapay_webhook_transaction_feedback(data)
        return True

    def _adapay_webhook_feedback(self, data):
        status = data["status"]
        # Update data
        self.write({
            "adapay_total_amount_sent": data["confirmedAmount"],
            "adapay_total_amount_left": data["pendingAmount"],
            "adapay_last_updated": data["updateTime"],
            "adapay_status": status,
        })
        for sale in self.sale_order_ids:
            sale.message_post(body=f"AdaPay payment update with data: {data}")
        if status == "confirmed":
            if self.state != "done":
                self._set_transaction_done()
        elif status in ["new", "payment-sent", "pending"]:
            if self.state != "pending":
                self._set_transaction_pending()
        else:
            if self.state != "cancel":
                self._set_transaction_cancel()
        return True

    def _adapay_webhook_transaction_feedback(self, data):
        # Update data
        for sale in self.sale_order_ids:
            sale.message_post(body=f"AdaPay transaction update with data: {data}")
        try:
            transaction_history = json.loads(self.adapay_transaction_history)
        except json.decoder.JSONDecodeError:
            transaction_history = {}
        transaction_history[data["hash"]] = data
        self.adapay_transaction_history = json.dumps(transaction_history)
        return True
