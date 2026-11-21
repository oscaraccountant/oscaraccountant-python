"""Small models and request specs for driving the runtime directly."""

from oscaraccountant.generated import OscarModel, Request


class Thing(OscarModel):
    id: str


class ThingPage(OscarModel):
    items: list[Thing]
    next_cursor: str | None


def spec(**changes: object) -> Request:
    values: dict[str, object] = {
        'operation_id': 'things_retrieve',
        'method': 'GET',
        'path': '/api/things/{thing_id}/',
        'path_params': {'thing_id': 't1'},
        'query': {},
        'body': None,
        'idempotent': False,
        'response': 'json',
    }
    values.update(changes)
    return Request(**values)  # type: ignore[arg-type]


def problem(code: str, status: int, detail: str = 'Something happened.') -> dict[str, object]:
    return {'type': 'about:blank', 'title': 'Error', 'status': status, 'code': code, 'detail': detail}


STORED = {
    'id': 'f1',
    'kind': 'purchase_invoice',
    'name': 'a.pdf',
    'content_type': 'application/pdf',
    'size_bytes': 4,
    'sha256': '0' * 64,
    'received_on': None,
    'core_document_id': None,
    'bank_transaction_ids': [],
    'uploaded_by': None,
    'created_at': '2026-09-27T10:00:00Z',
    'origin': None,
    'deleted_at': None,
}

COMPANY = {
    'id': 'c1',
    'name': 'Example OÜ',
    'registry_code': '12345678',
    'vat_number': '',
    'vat_status': 'not_registered',
    'vat_status_from': None,
    'legal_form': 'ou',
    'files_tsd_monthly': False,
    'state': 'ready',
    'core_company_id': None,
    'base_currency': 'EUR',
    'created_at': '2026-09-27T10:00:00Z',
    'updated_at': '2026-09-27T10:00:00Z',
}
