"""Literal values from explicitly labelled cells, without evaluating formulas."""

import re

from .models import FactInput

TABLE_EXTRACTOR = "literal-tables-v1"
NUMBER = re.compile(r"^[+-]?(?:\d{1,3}(?:[ .]\d{3})+|\d+)(?:[,.]\d+)?\s*(?:€|EUR|%|USD|\$)?$")


def metric(label: str, value: str) -> str | None:
    name = " ".join(label.upper().split())
    if name in {"TOTALE COSTO", "COSTO TOTALE", "TOTAL COST", "COSTI"}:
        return "reported_total_cost"
    if name in {"TOTALE RICAVO", "RICAVO TOTALE", "TOTAL REVENUE", "RICAVI"}:
        return "reported_total_revenue"
    if name in {"MARGINE", "%MARGINE", "MARGIN", "MARGIN %"}:
        return "reported_margin_percent" if "%" in value else "reported_margin_amount"
    if re.fullmatch(r"(?:\[LEGACY\s*\]\s*)?TARGET GIORNATE(?:\s*-\s*.+)?", name):
        return "reported_target_days"
    return None


def table_facts(content: str, section_label: str) -> list[FactInput]:
    """Column positions are from the connector's tab-separated text, not invented Excel cells."""
    lines = content.splitlines(keepends=True)
    sheet = section_label
    facts = []
    for index, line in enumerate(lines):
        if line.startswith("## Sheet: "):
            sheet = line.strip()
        cells = [c.strip() for c in line.rstrip("\r\n").split("\t")]

        def add(label, value, quote, sheet=sheet):
            prop = metric(label, value)
            subject = sheet + " / " + label
            if prop and NUMBER.fullmatch(value) and len(quote) <= 5_000 and len(subject) <= 300:
                facts.append(
                    FactInput(
                        subject=subject,
                        predicate=prop,
                        value=value,
                        certainty="explicit",
                        quote=quote,
                    )
                )

        for column in range(len(cells) - 1):
            add(cells[column], cells[column + 1], line.rstrip("\r\n"))
        # A horizontal header must contain multiple recognized metric labels. Values
        # must occupy exactly the same columns on the immediately following row.
        if sum(metric(c, "0") is not None for c in cells) >= 2 and index + 1 < len(lines):
            values = [c.strip() for c in lines[index + 1].rstrip("\r\n").split("\t")]
            quote = line + lines[index + 1].rstrip("\r\n")
            for column, label in enumerate(cells):
                if column < len(values):
                    add(label, values[column], quote)
    return facts
