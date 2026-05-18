#!/usr/bin/env python3
"""Extract ETH addresses from CryptoScamData.xlsx into a CSV file.

The script reads only Excel columns B and C from the first worksheet, keeps rows
where column C is "ETH", and writes those filtered rows to a CSV with Address
and Token headers.
"""

from __future__ import annotations

import argparse
import csv
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree


DEFAULT_INPUT = "CryptoScamData.xlsx"
DEFAULT_OUTPUT = "eth_addresses.csv"
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
ODR_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XML_NAMESPACES = {"main": MAIN_NS, "rel": REL_NS, "odr": ODR_NS}
CELL_REFERENCE_RE = re.compile(r"([A-Z]+)([0-9]+)")


def normalize_cell_value(value: object) -> str:
    """Return a stripped string representation of an Excel cell value."""
    if value is None:
        return ""
    return str(value).strip()


def read_xml_from_xlsx(xlsx_file: zipfile.ZipFile, member: str) -> ElementTree.Element:
    """Read an XML member from an XLSX archive."""
    with xlsx_file.open(member) as xml_file:
        return ElementTree.parse(xml_file).getroot()


def load_shared_strings(xlsx_file: zipfile.ZipFile) -> list[str]:
    """Return shared strings from an XLSX archive, if the file has any."""
    if "xl/sharedStrings.xml" not in xlsx_file.namelist():
        return []

    root = read_xml_from_xlsx(xlsx_file, "xl/sharedStrings.xml")
    shared_strings: list[str] = []

    for string_item in root.findall("main:si", XML_NAMESPACES):
        text_parts = [
            text_node.text or ""
            for text_node in string_item.findall(".//main:t", XML_NAMESPACES)
        ]
        shared_strings.append("".join(text_parts))

    return shared_strings


def first_worksheet_member(xlsx_file: zipfile.ZipFile) -> str:
    """Return the archive member path for the workbook's first worksheet."""
    workbook_root = read_xml_from_xlsx(xlsx_file, "xl/workbook.xml")
    relationship_root = read_xml_from_xlsx(xlsx_file, "xl/_rels/workbook.xml.rels")

    first_sheet = workbook_root.find("main:sheets/main:sheet", XML_NAMESPACES)
    if first_sheet is None:
        raise ValueError("The workbook does not contain any worksheets.")

    relationship_id = first_sheet.attrib[f"{{{ODR_NS}}}id"]
    for relationship in relationship_root.findall("rel:Relationship", XML_NAMESPACES):
        if relationship.attrib.get("Id") == relationship_id:
            target = relationship.attrib["Target"]
            return f"xl/{target.lstrip('/')}" if not target.startswith("xl/") else target

    raise ValueError(f"Could not find worksheet relationship: {relationship_id}")


def cell_column(cell_reference: str) -> str:
    """Return the column letters from an Excel cell reference, such as B1."""
    match = CELL_REFERENCE_RE.fullmatch(cell_reference)
    if match is None:
        return ""
    return match.group(1)


def cell_text(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    """Return a cell's display text for common XLSX cell storage types."""
    cell_type = cell.attrib.get("t")

    if cell_type == "inlineStr":
        return "".join(
            text_node.text or ""
            for text_node in cell.findall(".//main:t", XML_NAMESPACES)
        )

    value_node = cell.find("main:v", XML_NAMESPACES)
    if value_node is None or value_node.text is None:
        return ""

    value = value_node.text
    if cell_type == "s":
        return shared_strings[int(value)]
    return value


def extract_eth_rows(input_path: Path) -> list[tuple[str, str]]:
    """Return (address, token) rows where Excel column C contains ETH."""
    rows: list[tuple[str, str]] = []

    with zipfile.ZipFile(input_path) as xlsx_file:
        shared_strings = load_shared_strings(xlsx_file)
        worksheet_member = first_worksheet_member(xlsx_file)
        worksheet_root = read_xml_from_xlsx(xlsx_file, worksheet_member)

        for row in worksheet_root.findall(".//main:row", XML_NAMESPACES):
            selected_cells: dict[str, str] = {}
            for cell in row.findall("main:c", XML_NAMESPACES):
                column = cell_column(cell.attrib.get("r", ""))
                if column in {"B", "C"}:
                    selected_cells[column] = normalize_cell_value(
                        cell_text(cell, shared_strings)
                    )

            address = selected_cells.get("B", "")
            token = selected_cells.get("C", "")
            if token.upper() == "ETH":
                rows.append((address, token.upper()))

    return rows


def write_csv(rows: list[tuple[str, str]], output_path: Path) -> None:
    """Write filtered ETH rows to a CSV file."""
    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["Address", "Token"])
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read CryptoScamData.xlsx, keep rows where column C is ETH, "
            "and write Address/Token rows from columns B/C to CSV."
        )
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        default=DEFAULT_INPUT,
        help=f"Input XLSX file path (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "output_file",
        nargs="?",
        default=DEFAULT_OUTPUT,
        help=f"Output CSV file path (default: {DEFAULT_OUTPUT})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = Path(args.input_file)
    output_path = Path(args.output_file)

    if not input_path.is_file():
        raise FileNotFoundError(f"Input XLSX file not found: {input_path}")

    rows = extract_eth_rows(input_path)
    write_csv(rows, output_path)
    print(f"Wrote {len(rows)} ETH rows to {output_path}")


if __name__ == "__main__":
    main()
