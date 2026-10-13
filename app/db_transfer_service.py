"""Full-database JSON export/import for moving all data between Ledger instances."""
import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import Boolean, Date, DateTime, Numeric, inspect, select, text

from app import db

logger = logging.getLogger(__name__)

EXPORT_FORMAT = 'ledger-db-export'
EXPORT_VERSION = 1


def _serialize_value(value):
    if isinstance(value, Decimal):
        return str(value)  # str, not float, so balances round-trip exactly
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _coerce_value(column, value):
    if value is None:
        return None
    col_type = column.type
    if isinstance(col_type, Numeric):
        if isinstance(value, bool):
            raise ValueError('expected a number')
        try:
            return Decimal(str(value))
        except InvalidOperation:
            raise ValueError('expected a number') from None
    if isinstance(col_type, DateTime):
        return datetime.fromisoformat(value)
    if isinstance(col_type, Date):
        return date.fromisoformat(value)
    if isinstance(col_type, Boolean):
        if not isinstance(value, bool):
            raise ValueError('expected true or false')
        return value
    return value


def export_filename() -> str:
    return f'ledger_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'


def export_database() -> dict:
    # Read what the database actually has, not what the models declare: some tables and
    # columns were added without a migration, so older databases lack them. The importer
    # fills anything missing with empty tables and column defaults.
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    tables = {}
    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            logger.warning('Database export: table %s not in database, skipped', table.name)
            continue
        existing_cols = {col['name'] for col in inspector.get_columns(table.name)}
        columns = [col for col in table.columns if col.name in existing_cols]
        if len(columns) < len(table.columns):
            logger.warning('Database export: table %s missing columns %s, skipped',
                           table.name, sorted(set(table.columns.keys()) - existing_cols))
        rows = db.session.execute(select(*columns).order_by(*table.primary_key.columns)).mappings()
        tables[table.name] = [{k: _serialize_value(v) for k, v in row.items()} for row in rows]
    logger.info('Database export: tables=%d, rows=%d',
                len(tables), sum(len(rows) for rows in tables.values()))
    return {
        'format': EXPORT_FORMAT,
        'version': EXPORT_VERSION,
        'exported_at': datetime.now(timezone.utc).isoformat(),
        'tables': tables,
    }


def _parse_payload(payload) -> dict:
    """Validate and type-convert every row up front so a bad file never touches the existing data."""
    if not isinstance(payload, dict) or payload.get('format') != EXPORT_FORMAT:
        raise ValueError('Not a Ledger database export file.')
    if payload.get('version') != EXPORT_VERSION:
        raise ValueError(f'Unsupported export version: {payload.get("version")!r}.')
    tables_in = payload.get('tables')
    if not isinstance(tables_in, dict):
        raise ValueError('Export file has no "tables" section.')

    known = {table.name: table for table in db.metadata.tables.values()}
    unknown = sorted(set(tables_in) - set(known))
    if unknown:
        # Usually the export came from a newer Ledger version; dropping these silently would lose data.
        raise ValueError(f'Unknown tables in export: {", ".join(unknown)}. '
                         'Update this Ledger instance to the same version as the source.')

    parsed = {}
    for name, rows in tables_in.items():
        table = known[name]
        if not isinstance(rows, list):
            raise ValueError(f'Table "{name}" must be a list of rows.')
        parsed_rows = []
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f'Table "{name}" row {i} is not an object.')
            if row.get('id') is None:
                # Foreign keys in other rows refer to these ids, so they can't be regenerated.
                raise ValueError(f'Table "{name}" row {i} has no id.')
            unknown_cols = sorted(set(row) - set(table.columns.keys()))
            if unknown_cols:
                raise ValueError(f'Unknown columns in table "{name}": {", ".join(unknown_cols)}. '
                                 'Update this Ledger instance to the same version as the source.')
            parsed_row = {}
            for col_name, value in row.items():
                try:
                    parsed_row[col_name] = _coerce_value(table.columns[col_name], value)
                except (TypeError, ValueError) as e:
                    raise ValueError(f'Invalid value in table "{name}" row {i} column "{col_name}": {e}') from None
            parsed_rows.append(parsed_row)
        parsed[name] = parsed_rows
    return parsed


def _self_referencing_columns(table) -> list[str]:
    return [fk.parent.name for fk in table.foreign_keys if fk.column.table is table]


def _reset_postgres_sequences():
    """Explicit ids don't advance Postgres serial sequences, so the next insert would collide."""
    for table in db.metadata.sorted_tables:
        db.session.execute(text(
            f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
            f"COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table.name}"
        ))


def import_database(payload) -> dict:
    """Replace every row in the database with the export's contents; all-or-nothing."""
    parsed = _parse_payload(payload)
    counts = {}
    try:
        for table in reversed(db.metadata.sorted_tables):
            db.session.execute(table.delete())

        for table in db.metadata.sorted_tables:
            rows = parsed.get(table.name, [])
            counts[table.name] = len(rows)
            if not rows:
                continue
            # Rows can point at later rows of the same table (accounts.paired_liability_id),
            # so insert those links after every row exists.
            self_refs = _self_referencing_columns(table)
            # Row by row: a hand-edited file may omit columns on some rows, which a bulk insert rejects.
            for row in rows:
                db.session.execute(table.insert().values({**row, **dict.fromkeys(self_refs)}))
            for row in rows:
                links = {col: row[col] for col in self_refs if row.get(col) is not None}
                if links:
                    db.session.execute(table.update().where(table.c.id == row['id']).values(**links))

        if db.engine.dialect.name == 'postgresql':
            _reset_postgres_sequences()
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    logger.info('Database import: tables=%d, rows=%d', len(counts), sum(counts.values()))
    return counts

