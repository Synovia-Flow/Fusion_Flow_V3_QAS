from __future__ import annotations

import datetime as dt
import html
import os
import struct
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "Documentation_Layer" / "Technical_Details_Overview.docx"

IMAGE_CANDIDATES = [
    ROOT / "Documentation_Layer" / "Goods_Excel_Validation_Prototype" / "assets" / "synovia-flow-logo.png",
    Path(r"C:\Windows\Temp\Fusion_Flow_V2_BKD_prod_codex\app\static\stitch\synovia_flow_1.0.png"),
    Path(r"C:\Windows\Temp\Fusion_Flow_V2_BKD_prod_codex\docs\assets\fusion-flow-ing-staging-tss-map.png"),
    Path(r"C:\Windows\Temp\Fusion_Flow_V2_BKD_prod_codex\docs\screenshots\operations-flow.png"),
]

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
}


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def paragraph(text: str = "", style: str | None = None, bold: bool = False, italic: bool = False) -> str:
    style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    run_pr = ""
    if bold or italic:
        run_pr = "<w:rPr>" + ("<w:b/>" if bold else "") + ("<w:i/>" if italic else "") + "</w:rPr>"
    if not text:
        return f"<w:p>{style_xml}</w:p>"
    return f"<w:p>{style_xml}<w:r>{run_pr}<w:t>{esc(text)}</w:t></w:r></w:p>"


def page_break() -> str:
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def table(rows: list[list[str]], widths: list[int] | None = None, header: bool = True) -> str:
    if not rows:
        return ""
    cols = len(rows[0])
    widths = widths or [int(9000 / cols)] * cols
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    xml = [
        "<w:tbl>",
        "<w:tblPr>",
        '<w:tblStyle w:val="TableGrid"/>',
        '<w:tblW w:w="0" w:type="auto"/>',
        '<w:tblBorders><w:top w:val="single" w:sz="4" w:color="B8C6D9"/>'
        '<w:left w:val="single" w:sz="4" w:color="B8C6D9"/>'
        '<w:bottom w:val="single" w:sz="4" w:color="B8C6D9"/>'
        '<w:right w:val="single" w:sz="4" w:color="B8C6D9"/>'
        '<w:insideH w:val="single" w:sz="4" w:color="D7DEE8"/>'
        '<w:insideV w:val="single" w:sz="4" w:color="D7DEE8"/></w:tblBorders>',
        "</w:tblPr>",
        f"<w:tblGrid>{grid}</w:tblGrid>",
    ]
    for row_index, row in enumerate(rows):
        xml.append("<w:tr>")
        for col_index, cell in enumerate(row):
            fill = "1F4E79" if header and row_index == 0 else ("F4F7FB" if row_index % 2 == 0 else "FFFFFF")
            color = "FFFFFF" if header and row_index == 0 else "1F2933"
            bold_xml = "<w:b/>" if header and row_index == 0 else ""
            xml.append(
                "<w:tc>"
                f'<w:tcPr><w:tcW w:w="{widths[min(col_index, len(widths)-1)]}" w:type="dxa"/>'
                f'<w:shd w:fill="{fill}"/></w:tcPr>'
                "<w:p><w:r>"
                f'<w:rPr>{bold_xml}<w:color w:val="{color}"/></w:rPr>'
                f"<w:t>{esc(cell)}</w:t>"
                "</w:r></w:p>"
                "</w:tc>"
            )
        xml.append("</w:tr>")
    xml.append("</w:tbl>")
    xml.append(paragraph())
    return "".join(xml)


def png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
        if header[:8] == b"\x89PNG\r\n\x1a\n":
            width, height = struct.unpack(">II", header[16:24])
            return width, height
    except OSError:
        return None
    return None


def jpeg_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as handle:
            data = handle.read()
    except OSError:
        return None
    if not data.startswith(b"\xff\xd8"):
        return None
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        i += 2
        if marker in (0xD8, 0xD9):
            continue
        length = int.from_bytes(data[i : i + 2], "big")
        if marker in range(0xC0, 0xC4):
            height = int.from_bytes(data[i + 3 : i + 5], "big")
            width = int.from_bytes(data[i + 5 : i + 7], "big")
            return width, height
        i += length
    return None


def dimensions(path: Path) -> tuple[int, int]:
    return png_dimensions(path) or jpeg_dimensions(path) or (1200, 700)


def image_xml(rel_id: str, path: Path, image_index: int, max_width_inches: float = 6.4) -> str:
    width_px, height_px = dimensions(path)
    width_inches = min(max_width_inches, width_px / 96)
    height_inches = width_inches * height_px / max(width_px, 1)
    cx = int(width_inches * 914400)
    cy = int(height_inches * 914400)
    name = esc(path.name)
    return f"""
<w:p>
  <w:pPr><w:jc w:val="center"/></w:pPr>
  <w:r>
    <w:drawing>
      <wp:inline distT="0" distB="0" distL="0" distR="0">
        <wp:extent cx="{cx}" cy="{cy}"/>
        <wp:effectExtent l="0" t="0" r="0" b="0"/>
        <wp:docPr id="{image_index}" name="{name}"/>
        <wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>
        <a:graphic>
          <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
            <pic:pic>
              <pic:nvPicPr>
                <pic:cNvPr id="{image_index}" name="{name}"/>
                <pic:cNvPicPr/>
              </pic:nvPicPr>
              <pic:blipFill>
                <a:blip r:embed="{rel_id}"/>
                <a:stretch><a:fillRect/></a:stretch>
              </pic:blipFill>
              <pic:spPr>
                <a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>
                <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
              </pic:spPr>
            </pic:pic>
          </a:graphicData>
        </a:graphic>
      </wp:inline>
    </w:drawing>
  </w:r>
</w:p>
"""


def content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    return "application/octet-stream"


def existing_images() -> list[Path]:
    found: list[Path] = []
    for candidate in IMAGE_CANDIDATES:
        try:
            if candidate.exists() and candidate.is_file():
                found.append(candidate)
        except OSError:
            continue
    return found


def styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault><w:rPr><w:rFonts w:ascii="Aptos" w:hAnsi="Aptos"/><w:sz w:val="22"/><w:color w:val="1F2933"/></w:rPr></w:rPrDefault>
    <w:pPrDefault><w:pPr><w:spacing w:after="140" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:rPr><w:b/><w:color w:val="1F4E79"/><w:sz w:val="44"/></w:rPr><w:pPr><w:spacing w:after="160"/></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/><w:rPr><w:color w:val="5B6778"/><w:sz w:val="26"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:rPr><w:b/><w:color w:val="1F4E79"/><w:sz w:val="30"/></w:rPr><w:pPr><w:spacing w:before="260" w:after="120"/></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:rPr><w:b/><w:color w:val="243B53"/><w:sz w:val="25"/></w:rPr><w:pPr><w:spacing w:before="160" w:after="80"/></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="Caption"/><w:basedOn w:val="Normal"/><w:rPr><w:i/><w:color w:val="64748B"/><w:sz w:val="18"/></w:rPr><w:pPr><w:jc w:val="center"/></w:pPr></w:style>
  <w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4" w:color="B8C6D9"/><w:left w:val="single" w:sz="4" w:color="B8C6D9"/><w:bottom w:val="single" w:sz="4" w:color="B8C6D9"/><w:right w:val="single" w:sz="4" w:color="B8C6D9"/><w:insideH w:val="single" w:sz="4" w:color="D7DEE8"/><w:insideV w:val="single" w:sz="4" w:color="D7DEE8"/></w:tblBorders></w:tblPr></w:style>
</w:styles>
"""


def settings_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:zoom w:percent="100"/>
  <w:defaultTabStop w:val="720"/>
</w:settings>
"""


def core_xml() -> str:
    today = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>Technical Details Overview</dc:title>
  <dc:subject>Fusion Flow V3 QAS architecture and stack proposal</dc:subject>
  <dc:creator>Synovia Digital Engineering</dc:creator>
  <cp:lastModifiedBy>Synovia Digital Engineering</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{today}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{today}</dcterms:modified>
</cp:coreProperties>
"""


def app_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Microsoft Word</Application>
  <DocSecurity>0</DocSecurity>
  <ScaleCrop>false</ScaleCrop>
  <Company>Synovia Digital</Company>
</Properties>
"""


def document_body(images: list[Path]) -> tuple[str, list[tuple[str, Path]]]:
    rel_images: list[tuple[str, Path]] = []
    xml: list[str] = []

    xml.append(paragraph("Technical Details Overview", "Title"))
    xml.append(paragraph("Fusion Flow V3 QAS - Architecture and Stack Alignment Proposal", "Subtitle"))
    xml.append(paragraph("Status: Draft for team agreement", bold=True))
    xml.append(paragraph("Project: Fusion Flow V3 QAS"))
    xml.append(paragraph("Date: June 2026"))
    xml.append(paragraph("Purpose: align the team before implementation starts, without locking decisions that still require confirmation."))

    if images:
        rel_id = "rIdImage1"
        rel_images.append((rel_id, images[0]))
        xml.append(image_xml(rel_id, images[0], 1, max_width_inches=3.2))
    xml.append(page_break())

    xml.append(paragraph("1. Purpose", "Heading1"))
    xml.append(paragraph(
        "This document sets out the proposed architecture and technology stack for Fusion Flow V3 QAS. "
        "It is intended to support team alignment before development starts. It does not assume final approval; each major decision is framed as a recommendation to confirm."
    ))
    xml.append(paragraph(
        "The scope is architecture, maintainability, operating cost, supportability and delivery risk. "
        "Detailed functional requirements remain separate from this document."
    ))

    xml.append(paragraph("2. Current Context", "Heading1"))
    xml.append(paragraph("Reference: github.com/Synovia-Digital/Fusion_Flow_V2_BKD  —  branch: prod", italic=True))
    xml.append(table([
        ["Input", "Observed Direction", "Why It Matters"],
        ["Fusion Flow V2 BKD prod", "Flask, Jinja templates, native HTML/CSS/JS, Azure SQL via pyodbc, Docker/Gunicorn, Render services and scheduled jobs. Repo: github.com/Synovia-Digital/Fusion_Flow_V2_BKD (branch: prod).", "This is the closest working reference and reduces delivery risk if V3 follows familiar patterns."],
        ["Fusion Flow V3 QAS", "Layered folders, Graph ingestion scripts, SQL Server schemas, minimal Flask health app, native Goods Excel validation prototype.", "The new repo already points toward a lightweight Python service model with DB-first configuration."],
        ["FRD style and operating model", "Formal numbered sections, clear requirements, acceptance criteria and future recommendations.", "The V3 technical overview should stay concise, auditable and easy to approve."],
    ], [2200, 3600, 3600]))

    xml.append(paragraph("3. Design Principles", "Heading1"))
    for item in [
        "Maintain the smallest viable stack that can support production operations.",
        "Prefer existing proven patterns from V2 where they still fit V3.",
        "Keep tenant-specific behavior in configuration first, not hard-coded branches.",
        "Separate source trace, working state and official TSS mirror data.",
        "Build for support visibility from phase 1, because automated flows are only maintainable when failures are explainable.",
        "Avoid adopting a larger frontend or API framework until the product surface requires it.",
    ]:
        xml.append(paragraph(f"- {item}"))

    xml.append(paragraph("4. Architecture Overview", "Heading1"))
    xml.append(paragraph(
        "The proposed V3 architecture follows the same business truth model as the existing Flow work: inbound data is captured, traced, transformed into working records, submitted or synced with TSS, and then exposed through support and operational views."
    ))
    if len(images) > 2:
        rel_id = "rIdImage2"
        rel_images.append((rel_id, images[2]))
        xml.append(image_xml(rel_id, images[2], 2, max_width_inches=6.4))
        xml.append(paragraph("Reference visual: existing Fusion Flow ING/STG/TSS architecture map from the V2 production material.", "Caption"))
    xml.append(table([
        ["Layer", "Purpose", "V3 Direction"],
        ["Documentation Layer", "Explains why the system exists and what has been agreed.", "Architecture notes, operating model, decision records, runbooks and requirements."],
        ["Configuration Layer", "Owns database contracts and tenant/runtime configuration.", "SQL migrations, seeds, tenant settings, Graph routes, pack rules and environment gates."],
        ["Integration Layer", "Owns executable services and automation entry points.", "Flask app, FLOW_V3 scripts, Graph ingestion, TSS client, validation and notifications."],
        ["Infrastructure Layer", "Owns deployable runtime shape.", "Azure definitions first, Render/Docker only where useful for QAS or temporary hosting."],
    ], [2200, 3400, 3600]))

    xml.append(paragraph("5. Stack Decision Areas", "Heading1"))
    decision_rows = [
        ["AD-001", "Infrastructure", "Azure as the target platform; keep Render as optional QAS/demo fallback.", "Azure aligns with Microsoft Graph, Azure SQL, Key Vault, App Insights and storage. Render is useful for quick container deployment but should not drive the long-term architecture."],
        ["AD-002", "Backend", "Use Flask with blueprints and service modules.", "V2 prod and V3 QAS already use Flask. It is simple, maintainable and sufficient for server-rendered operational tooling."],
        ["AD-003", "Frontend", "Use native HTML/CSS/JS with Jinja templates for phase 1.", "The product is operational rather than consumer-style SPA. Avoiding React/Angular reduces build complexity, dependency churn and support overhead."],
        ["AD-004", "Python Runtime", "Standardise on Python 3.12.", "Matches V2 production direction and keeps runtime behavior consistent across services and jobs."],
        ["AD-005", "Database", "Use Azure SQL / SQL Server with schemas CFG, EXC, ING, STG and TSS.", "The layered data model supports traceability, support diagnostics, official TSS mirrors and future tenant expansion."],
        ["AD-006", "ODBC Driver", "Use ODBC Driver 18 as the new standard; keep Driver 17 as fallback only.", "V3 configuration already points to Driver 18. Keeping Driver 17 as fallback protects local compatibility while moving forward."],
        ["AD-007", "Support Portal", "Include support/technical views from phase 1.", "Automated ingestion and TSS submission need health checks, queues, logs, retry paths and failure explanations to remain supportable."],
        ["AD-008", "Analytics", "Start with operational analytics only.", "Volumes, errors, latency and job status are useful immediately. Full BI should wait until data contracts stabilise."],
        ["AD-009", "Jobs", "Use explicit FLOW_V3 scripts and scheduled jobs before introducing heavier orchestration.", "The current process naturally maps to steps 01-05. Simple jobs are cheaper to operate and easier to debug in QAS."],
        ["AD-010", "Configuration and Secrets", "DB-first configuration, environment/Key Vault for secrets, YAML only as transition/dev fallback.", "This makes tenant changes auditable and reduces the risk of secrets or tenant rules being buried in code."],
    ]
    xml.append(table([["ID", "Area", "Recommendation", "Rationale"]] + decision_rows, [1000, 1800, 3100, 3500]))

    xml.append(paragraph("6. Maintainability and Commercial Rationale", "Heading1"))
    xml.append(paragraph(
        "The recommended approach is commercially sensible because it reuses proven delivery patterns, keeps the runtime small, and avoids paying the complexity cost of frameworks or services that are not yet required."
    ))
    xml.append(table([
        ["Decision", "Cost / Maintenance Benefit"],
        ["Flask over FastAPI for phase 1", "Fewer moving parts, direct reuse of V2 patterns, simpler onboarding and easier support for server-rendered workflows."],
        ["Native HTML/CSS/JS over React/Angular", "No Node build pipeline required for the core portal, fewer dependencies, less upgrade risk and faster changes for operational screens."],
        ["Azure SQL schemas by responsibility", "Clear ownership of configuration, execution trace, inbound evidence, working data and official API mirrors."],
        ["Support portal from the start", "Reduces incident handling time because failures can be traced without manually reading logs, folders and database rows separately."],
        ["Operational analytics first", "Gives immediate value to support and management while postponing BI investment until metrics are stable."],
        ["DB-first tenant configuration", "New customers and route changes can be managed without code changes once the configuration model is mature."],
    ], [3200, 6200]))

    xml.append(paragraph("7. Proposed Project Structure", "Heading1"))
    xml.append(paragraph("Integration_Layer/Portal separates backend Python from frontend views and assets.", italic=True))
    structure = """Fusion_Flow_V3_QAS/
  Documentation_Layer/
  Configuration_Layer/
    SQL/
      migrations/
      seeds/
  Integration_Layer/
    Portal/
      fusion_portal/         <- backend: Flask Python package
        blueprints/          <- route handlers per domain
        helpers/             <- shared utilities
        ingestion/           <- ingestion pipeline modules
        services/            <- business logic
      templates/             <- frontend: Jinja views (.html)
      static/                <- frontend: CSS, JS, images
      export_templates/      <- Excel / Word export templates
      wsgi.py
    FLOW_V3/                 <- pipeline scripts 01-05
    Graph/                   <- Graph email ingestion
    <tenant>/                <- per-tenant file workspace
      Inbound/
      Process/
  Infrastructure_Layer/
    Azure/
    Render/
    Docker/
  scripts/
  tests/"""
    for line in structure.splitlines():
        xml.append(paragraph(line))

    xml.append(paragraph("8. Open Decisions for Team Agreement", "Heading1"))
    xml.append(table([
        ["Question", "Proposed Answer", "Team Decision"],
        ["Should Azure be the target infrastructure?", "Yes. Render remains optional for QAS/demo.", "Pending"],
        ["Should the backend remain Flask?", "Yes, unless a future public API surface requires FastAPI.", "Pending"],
        ["Should the frontend remain native HTML/CSS/JS for now?", "Yes. React/Angular can be reconsidered if the UI becomes SPA-heavy.", "Pending"],
        ["Should a support portal be included in phase 1?", "Yes. It is essential for maintainability and operational confidence.", "Pending"],
        ["Should analytics be included?", "Yes, but operational analytics only in phase 1.", "Pending"],
        ["Should tenant configuration move DB-first?", "Yes, with YAML retained only as a transition/dev fallback.", "Pending"],
    ], [4200, 3600, 1600]))

    xml.append(paragraph("9. Recommended Next Steps", "Heading1"))
    for item in [
        "Confirm or challenge each open decision in the team discussion.",
        "Rename Integration_Layer/App to Integration_Layer/Portal and reorganise into fusion_portal/ (backend) + templates/ + static/ (frontend).",
        "Move Jinja HTML views into Portal/templates/ and static assets into Portal/static/.",
        "Move Excel/Word export templates from app/static/templates/ into Portal/export_templates/.",
        "Create the Infrastructure_Layer folder with Azure, Render and Docker placeholders.",
        "Split requirements into base, portal, jobs and optional AI/document-intelligence files.",
        "Keep FLOW_V3 scripts numbered 01-05 and FLOW_V3/Run_History as-is.",
        "Add decision records when the team confirms or changes the proposed stack.",
    ]:
        xml.append(paragraph(f"- {item}"))

    xml.append(paragraph("10. Conclusion", "Heading1"))
    xml.append(paragraph(
        "The recommended stack prioritises delivery speed, operational visibility and maintainability. "
        "It keeps V3 close to the proven V2 production shape while allowing the team to evolve toward Azure-native hosting, DB-first configuration and stronger support tooling. "
        "The main decision for the team is whether this conservative, support-first architecture is the right base for starting implementation."
    ))

    section = """
<w:sectPr>
  <w:pgSz w:w="11906" w:h="16838"/>
  <w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="708" w:footer="708" w:gutter="0"/>
  <w:cols w:space="708"/>
  <w:docGrid w:linePitch="360"/>
</w:sectPr>
"""
    return "".join(xml) + section, rel_images


def document_xml(body: str) -> str:
    ns_attrs = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items())
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document {ns_attrs}>
  <w:body>{body}</w:body>
</w:document>
"""


def rels_xml(image_rels: list[tuple[str, Path]]) -> str:
    rels = [
        '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>',
        '<Relationship Id="rIdSettings" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>',
    ]
    for rel_id, path in image_rels:
        media_name = f"media/{path.name}"
        rels.append(
            f'<Relationship Id="{rel_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="{esc(media_name)}"/>'
        )
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
""" + "\n".join(rels) + "\n</Relationships>\n"


def root_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""


def content_types_xml(image_rels: list[tuple[str, Path]]) -> str:
    defaults = {
        "rels": "application/vnd.openxmlformats-package.relationships+xml",
        "xml": "application/xml",
    }
    image_defaults: dict[str, str] = {}
    for _, path in image_rels:
        suffix = path.suffix.lower().lstrip(".")
        image_defaults[suffix] = content_type_for(path)
    default_xml = "".join(f'<Default Extension="{ext}" ContentType="{ctype}"/>' for ext, ctype in {**defaults, **image_defaults}.items())
    overrides = """
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
"""
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
{default_xml}
{overrides}
</Types>
"""


def main() -> None:
    images = existing_images()
    body, image_rels = document_body(images)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", content_types_xml(image_rels))
        docx.writestr("_rels/.rels", root_rels_xml())
        docx.writestr("docProps/core.xml", core_xml())
        docx.writestr("docProps/app.xml", app_xml())
        docx.writestr("word/document.xml", document_xml(body))
        docx.writestr("word/styles.xml", styles_xml())
        docx.writestr("word/settings.xml", settings_xml())
        docx.writestr("word/_rels/document.xml.rels", rels_xml(image_rels))
        used_names: set[str] = set()
        for _, path in image_rels:
            media_name = path.name
            if media_name in used_names:
                stem = path.stem
                suffix = path.suffix
                index = 2
                while f"{stem}_{index}{suffix}" in used_names:
                    index += 1
                media_name = f"{stem}_{index}{suffix}"
            used_names.add(media_name)
            docx.write(path, f"word/media/{media_name}")
    print(OUT)


if __name__ == "__main__":
    main()
