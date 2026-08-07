"""Typed document artifacts shared by acquisition and extraction.

URLs are source identities, not file types.  This module keeps the facts
learned from bytes (format, media type, content hash, and safe temporary-file
suffix) together so callers do not have to rediscover them from extensions or
HTTP headers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import html as html_module
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from parsing.pdf import detect_document_format
from pipeline.utils import attachment_identity


class DocumentFormat(str, Enum):
    PDF = "pdf"
    HTML = "html"
    DOC = "doc"
    DOCX = "docx"
    RTF = "rtf"
    PPTX = "pptx"
    XLSX = "xlsx"
    UNKNOWN = "unknown"


_FORMAT_MEDIA_TYPES = {
    DocumentFormat.PDF: "application/pdf",
    DocumentFormat.HTML: "text/html; charset=utf-8",
    DocumentFormat.DOC: "application/msword",
    DocumentFormat.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    DocumentFormat.RTF: "application/rtf",
    DocumentFormat.PPTX: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    DocumentFormat.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    DocumentFormat.UNKNOWN: "application/octet-stream",
}

_FORMAT_SUFFIXES = {
    DocumentFormat.PDF: ".pdf",
    DocumentFormat.HTML: ".html",
    DocumentFormat.DOC: ".doc",
    DocumentFormat.DOCX: ".docx",
    DocumentFormat.RTF: ".rtf",
    DocumentFormat.PPTX: ".pptx",
    DocumentFormat.XLSX: ".xlsx",
    DocumentFormat.UNKNOWN: ".bin",
}

_SUPPORTED_EXTENSIONS = (".pdf", ".doc", ".docx", ".rtf", ".pptx", ".xlsx")
_VENDOR_DOCUMENT_PATHS = (
    "/viewfile/",
    "/documentcenter/view/",
    "/linkclick.aspx",
    "/metaviewer.php",
    "/documents/viewdocument/",
    "/documents/downloadfilebytes/",
)
_HTML_PREFIX_RE = re.compile(br"^\s*(?:<!doctype\s+html\b|<html\b)", re.IGNORECASE)
_SPACE_RE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n(?:\s*\n)+")


def _content_type_base(content_type: str | None) -> str:
    return (content_type or "").partition(";")[0].strip().lower()


def sniff_document_format(
    data: bytes,
    *,
    content_type: str | None = None,
    source_url: str | None = None,
) -> DocumentFormat:
    """Identify a supported document from bytes, then conservative metadata.

    Magic wins over server headers and URL suffixes.  Several municipal hosts
    label every download as ``application/octet-stream`` or return signed URLs
    without extensions, so metadata is only a fallback when the bytes are not
    self-identifying.
    """
    detected = detect_document_format(data)
    if detected != "unknown":
        return DocumentFormat(detected)

    media_type = _content_type_base(content_type)
    if media_type in {"text/html", "application/xhtml+xml"} or _HTML_PREFIX_RE.match(data[:512]):
        return DocumentFormat.HTML

    media_hints = {
        "application/pdf": DocumentFormat.PDF,
        "application/msword": DocumentFormat.DOC,
        "application/rtf": DocumentFormat.RTF,
        "text/rtf": DocumentFormat.RTF,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DocumentFormat.DOCX,
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": DocumentFormat.PPTX,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": DocumentFormat.XLSX,
    }
    if media_type in media_hints:
        return media_hints[media_type]

    path = urlparse(source_url or "").path.lower()
    for fmt in (
        DocumentFormat.DOCX,
        DocumentFormat.PPTX,
        DocumentFormat.XLSX,
        DocumentFormat.PDF,
        DocumentFormat.DOC,
        DocumentFormat.RTF,
    ):
        if path.endswith(_FORMAT_SUFFIXES[fmt]):
            return fmt
    return DocumentFormat.UNKNOWN


def media_type_for(document_format: DocumentFormat) -> str:
    return _FORMAT_MEDIA_TYPES[document_format]


def suffix_for(document_format: DocumentFormat) -> str:
    return _FORMAT_SUFFIXES[document_format]


@dataclass(frozen=True, slots=True)
class DocumentArtifact:
    """Immutable acquisition result at the extraction boundary."""

    requested_url: str
    source_url: str
    data: bytes
    content_sha256: str
    document_format: DocumentFormat
    media_type: str
    from_corpus: bool = False

    @property
    def source_identity(self) -> str:
        return attachment_identity(self.source_url)

    @property
    def suffix(self) -> str:
        return suffix_for(self.document_format)


def make_artifact(
    *,
    requested_url: str,
    source_url: str,
    data: bytes,
    content_sha256: str,
    content_type: str | None = None,
    from_corpus: bool = False,
) -> DocumentArtifact:
    document_format = sniff_document_format(
        data,
        content_type=content_type,
        source_url=source_url,
    )
    return DocumentArtifact(
        requested_url=requested_url,
        source_url=source_url,
        data=data,
        content_sha256=content_sha256,
        document_format=document_format,
        media_type=media_type_for(document_format),
        from_corpus=from_corpus,
    )


def extract_document_links(html_bytes: bytes, base_url: str) -> list[str]:
    """Return supported download candidates from an HTML attachment page.

    Direct file links precede vendor viewer endpoints.  The result is stable,
    absolute, deduplicated, and excludes active/non-HTTP schemes.
    """
    soup = BeautifulSoup(html_bytes, "lxml")
    direct: list[str] = []
    vendor: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = html_module.unescape(str(anchor.get("href") or "").strip())
        if not href or href.startswith(("#", "javascript:", "data:", "mailto:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        identity = attachment_identity(absolute)
        if identity in seen:
            continue
        path = parsed.path.lower()
        target = direct if path.endswith(_SUPPORTED_EXTENSIONS) else vendor
        if target is vendor and not (
            any(marker in path for marker in _VENDOR_DOCUMENT_PATHS)
            or "cloudfront.net" in parsed.netloc.lower()
            or "s3.amazonaws.com" in parsed.netloc.lower()
        ):
            continue
        seen.add(identity)
        target.append(absolute)
    return direct + vendor


def sanitize_html_text(html_bytes: bytes) -> str:
    """Extract readable fallback text without scripts, chrome, or hidden UI."""
    soup = BeautifulSoup(html_bytes, "lxml")
    for tag in soup.find_all(
        ["script", "style", "noscript", "svg", "template", "nav", "header", "footer", "form"]
    ):
        tag.decompose()
    for tag in soup.select("[hidden], [aria-hidden='true']"):
        tag.decompose()

    root = soup.body or soup
    lines: list[str] = []
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if title:
        lines.append(title)
    for raw_line in root.get_text("\n").splitlines():
        line = _SPACE_RE.sub(" ", raw_line).strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


_TLS_BYPASS_HOST = "s3.amazonaws.com"
_TLS_BYPASS_PATH_PREFIX = "/granicus_production_attachments/"


def verify_tls_for_url(url: str) -> bool:
    """Verify TLS except for the one documented legacy Granicus S3 bucket."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return True
    return not (
        (parsed.hostname or "").lower() == _TLS_BYPASS_HOST
        and parsed.path.lower().startswith(_TLS_BYPASS_PATH_PREFIX)
    )

