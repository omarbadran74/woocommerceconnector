from __future__ import unicode_literals
import frappe
from frappe import _
import json, math, time, pytz
from .exceptions import woocommerceError
from frappe.utils import get_request_session, get_datetime
from frappe.client import get_time_zone
from woocommerce import API
from .utils import make_woocommerce_log
import requests
from frappe.utils import cint

_per_page = 100


# -----------------------------
# Helpers
# -----------------------------
def _normalize_verify_ssl(value):
    """
    Normalize WooCommerce verify_ssl config to a valid `requests` verify parameter.
    Returns either:
      - True / False (bool)
      - a string path (CA bundle) if clearly a path-like
    """
    # String: may be a path or a boolean-ish string
    if isinstance(value, str):
        v = value.strip()
        # Path-like?
        if "/" in v or "\\" in v or v.lower().endswith((".pem", ".crt", ".cer", ".der")):
            return v
        # Boolean-ish?
        return v.lower() in ("1", "true", "yes", "on")

    # None → default secure
    if value is None:
        return True

    # Int/Bool → cast to bool
    if isinstance(value, (int, bool)):
        return bool(value)

    # Fallback
    return True


def _safe_response_body(resp):
    """Return a safe string body for logging (JSON or text)."""
    try:
        return resp.json()
    except Exception:
        try:
            return resp.text
        except Exception:
            return "<no-body>"


def get_woocommerce_settings():
    d = frappe.get_doc("WooCommerce Config")
    if d.woocommerce_url:
        d.api_secret = d.get_password(fieldname='api_secret')
        return d.as_dict()
    else:
        frappe.throw(_("woocommerce store URL is not configured on WooCommerce Config"), woocommerceError)


def get_wcapi(settings=None, timeout=1000, query_string_auth=True):
    """
    Create a WooCommerce API client with normalized verify_ssl.
    """
    if not settings:
        settings = get_woocommerce_settings()

    verify_param = _normalize_verify_ssl(settings.get('verify_ssl'))

    wcapi = API(
        url=settings['woocommerce_url'],
        consumer_key=settings['api_key'],
        consumer_secret=settings['api_secret'],
        verify_ssl=verify_param,         # <- normalized (bool or CA bundle path)
        wp_api=True,
        version="wc/v3",
        timeout=timeout,
        query_string_auth=query_string_auth
    )
    return wcapi


# -----------------------------
# Core request wrappers
# -----------------------------
def get_request_request(path, settings=None):
    if not settings:
        settings = get_woocommerce_settings()

    wcapi = get_wcapi(settings=settings, timeout=1000, query_string_auth=True)
    r = wcapi.get(path)

    # Manually handle status for richer logs
    if r.status_code != requests.codes.ok:
        make_woocommerce_log(
            title="WooCommerce GET error {0}".format(r.status_code),
            status="Error",
            method="get_request",
            message="{0}: {1}".format(r.url, _safe_response_body(r)),
            request_data={"path": path},
            exception=True
        )
    return r


def get_request(path, settings=None):
    return get_request_request(path, settings).json()


def post_request(path, data):
    settings = get_woocommerce_settings()
    wcapi = get_wcapi(settings=settings, timeout=1000, query_string_auth=True)
    r = wcapi.post(path, data)

    if r.status_code not in (requests.codes.ok, requests.codes.created):
        make_woocommerce_log(
            title="WooCommerce POST error {0}".format(r.status_code),
            status="Error",
            method="post_request",
            message="{0}: {1}".format(r.url, _safe_response_body(r)),
            request_data=data,
            exception=True
        )
    return _safe_response_body(r)


def put_request(path, data):
    settings = get_woocommerce_settings()
    wcapi = get_wcapi(settings=settings, timeout=5000, query_string_auth=True)
    r = wcapi.put(path, data)

    if r.status_code not in (requests.codes.ok, requests.codes.created):
        make_woocommerce_log(
            title="WooCommerce PUT error {0}".format(r.status_code),
            status="Error",
            method="put_request",
            message="{0}: {1}".format(r.url, _safe_response_body(r)),
            request_data=data,
            exception=True
        )
    return _safe_response_body(r)


def delete_request(path):
    settings = get_woocommerce_settings()
    wcapi = get_wcapi(settings=settings, timeout=1000, query_string_auth=True)
    r = wcapi.delete(path)

    if r.status_code != requests.codes.ok:
        make_woocommerce_log(
            title="WooCommerce DELETE error {0}".format(r.status_code),
            status="Error",
            method="delete_request",
            message="{0}: {1}".format(r.url, _safe_response_body(r)),
            request_data={"path": path},
            exception=True
        )
    return _safe_response_body(r)


# -----------------------------
# Utilities
# -----------------------------
def get_woocommerce_url(path, settings):
    return settings['woocommerce_url']


def get_header(settings):
    # REST uses JSON content-type by default in woocommerce lib,
    # keep for compatibility if needed elsewhere.
    header = {'Content-Type': 'application/json'}
    return header


def get_filtering_condition():
    woocommerce_settings = get_woocommerce_settings()
    if woocommerce_settings.get("last_sync_datetime"):
        last_sync_datetime = get_datetime(woocommerce_settings["last_sync_datetime"])
        return "modified_after={0}".format(last_sync_datetime.isoformat())
    return ''


# -----------------------------
# Domain-specific wrappers
# -----------------------------
def get_country():
    # NOTE: path kept as-is per your original codebase
    return get_request('/admin/countries.json')['countries']


def get_woocommerce_items(ignore_filter_conditions=False):
    woocommerce_products = []

    filter_condition = ''
    if not ignore_filter_conditions:
        filter_condition = get_filtering_condition()
        if cint(frappe.get_value("WooCommerce Config", "WooCommerce Config", "sync_only_published")) == 1:
            # append with correct separator
            filter_condition = (filter_condition + "&" if filter_condition else "") + "status=publish"

    first_path = 'products?per_page={0}{1}'.format(
        _per_page,
        ('&' + filter_condition) if filter_condition else ''
    )
    response = get_request_request(first_path)
    woocommerce_products.extend(response.json())

    # Fix pagination header parsing: avoid int(None)
    total_pages = int(response.headers.get('X-WP-TotalPages') or 1)
    for page_idx in range(2, total_pages + 1):
        path = 'products?per_page={0}&page={1}{2}'.format(
            _per_page,
            page_idx,
            ('&' + filter_condition) if filter_condition else ''
        )
        response = get_request_request(path)
        woocommerce_products.extend(response.json())

    return woocommerce_products


def get_woocommerce_item_variants(woocommerce_product_id):
    woocommerce_product_variants = []

    path = 'products/{0}/variations?per_page={1}'.format(woocommerce_product_id, _per_page)
    response = get_request_request(path)
    woocommerce_product_variants.extend(response.json())

    total_pages = int(response.headers.get('X-WP-TotalPages') or 1)
    for page_idx in range(2, total_pages + 1):
        path = 'products/{0}/variations?per_page={1}&page={2}'.format(woocommerce_product_id, _per_page, page_idx)
        response = get_request_request(path)
        woocommerce_product_variants.extend(response.json())

    return woocommerce_product_variants


def get_woocommerce_item_image(woocommerce_product_id):
    return get_request("products/{0}".format(woocommerce_product_id))["images"]


def get_woocommerce_tax(woocommerce_tax_id):
    return get_request("taxes/{0}".format(woocommerce_tax_id))


def get_woocommerce_customer(woocommerce_customer_id):
    return get_request("customers/{0}".format(woocommerce_customer_id))


def get_woocommerce_orders(order_status):
    woocommerce_orders = []

    first_path = 'orders?per_page={0}&status={1}'.format(_per_page, order_status)
    response = get_request_request(first_path)
    woocommerce_orders.extend(response.json())

    total_pages = int(response.headers.get('X-WP-TotalPages') or 1)
    for page_idx in range(2, total_pages + 1):
        path = 'orders?per_page={0}&page={1}&status={2}'.format(_per_page, page_idx, order_status)
        response = get_request_request(path)
        woocommerce_orders.extend(response.json())

    return woocommerce_orders


def get_woocommerce_customers(ignore_filter_conditions=False):
    woocommerce_customers = []
    filter_condition = ''

    if not ignore_filter_conditions:
        filter_condition = get_filtering_condition()

        first_path = 'customers?per_page={0}{1}'.format(
            _per_page,
            ('&' + filter_condition) if filter_condition else ''
        )
        response = get_request_request(first_path)
        woocommerce_customers.extend(response.json())

        total_pages = int(response.headers.get('X-WP-TotalPages') or 1)
        for page_idx in range(2, total_pages + 1):
            path = 'customers?per_page={0}&page={1}{2}'.format(
                _per_page,
                page_idx,
                ('&' + filter_condition) if filter_condition else ''
            )
            response = get_request_request(path)
            woocommerce_customers.extend(response.json())

    return woocommerce_customers
