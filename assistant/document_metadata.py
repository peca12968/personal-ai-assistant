"""Describe stored text projections without importing their parser libraries."""
from . import docx_text,pdf_text


def metadata(document):
    return docx_text.metadata(document) or pdf_text.metadata(document)
