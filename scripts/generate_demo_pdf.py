"""
Generate docs/DEMO_SCRIPT.pdf from docs/DEMO_SCRIPT.md
with the Synovia Digital logo in the top-right header.

Usage:
    python scripts/generate_demo_pdf.py
"""
import os, re, copy

ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MD_PATH   = os.path.join(ROOT, 'docs', 'DEMO_SCRIPT.md')
LOGO_PATH = os.path.join(ROOT, 'app', 'static', 'img', 'synovia_logo.jpg')
OUT_PATH  = os.path.join(ROOT, 'docs', 'DEMO_SCRIPT.pdf')

from fpdf import FPDF

BRAND_RED  = (192, 57, 43)
DARK       = (30,  30,  30)
MID        = (80,  80,  80)
LIGHT_GREY = (245, 245, 245)
RULE_GREY  = (210, 210, 210)
TABLE_HEAD = (230, 230, 230)
TEAL       = (14, 165, 160)

MARGIN = 18
LOGO_W = 38
LOGO_H = 14

# ── unicode sanitiser ────────────────────────────────────────────────────────

_UNICODE_SUBS = {
    '—': '--', '–': '-',
    '‘': "'",  '’': "'",
    '“': '"',  '”': '"',
    '•': '*',  '…': '...',
    '·': '.',  '→': '->',
    'é': 'e',  'à': 'a',
    'ó': 'o',
}

def _sanitize(text):
    for ch, rep in _UNICODE_SUBS.items():
        text = text.replace(ch, rep)
    return text.encode('latin-1', 'replace').decode('latin-1')


# ── block parser ─────────────────────────────────────────────────────────────

def _split_into_blocks(lines):
    """
    Split markdown lines into blocks.
    Each block is a list of lines.
    Blocks split on every '## Scene' heading — those headings must not
    be broken across pages.  Non-scene sections stay in surrounding flow.
    Returns list of (is_scene, lines) tuples.
    """
    blocks = []
    current = []
    current_is_scene = False

    for line in lines:
        if re.match(r'^## Scene\b', line):
            if current:
                blocks.append((current_is_scene, current))
            current = [line]
            current_is_scene = True
        elif re.match(r'^## ', line) and current_is_scene:
            # non-scene H2 ends the previous scene block
            if current:
                blocks.append((True, current))
            current = [line]
            current_is_scene = False
        else:
            current.append(line)

    if current:
        blocks.append((current_is_scene, current))

    return blocks


# ── PDF class ────────────────────────────────────────────────────────────────

class DemoPDF(FPDF):
    def __init__(self):
        super().__init__(orientation='P', unit='mm', format='A4')
        self.set_margins(MARGIN, 22, MARGIN)
        self.set_auto_page_break(auto=True, margin=18)
        self.add_page()
        self._in_table   = False
        self._table_cols = []
        self._table_rows = []

    # ── header / footer ──────────────────────────────────────────────────────

    def header(self):
        if os.path.exists(LOGO_PATH):
            x = self.w - MARGIN - LOGO_W
            self.image(LOGO_PATH, x=x, y=6, w=LOGO_W, h=LOGO_H,
                       keep_aspect_ratio=True)
        self.set_draw_color(*RULE_GREY)
        self.set_line_width(0.3)
        self.line(MARGIN, 22, self.w - MARGIN, 22)

    def footer(self):
        self.set_y(-13)
        self.set_draw_color(*RULE_GREY)
        self.set_line_width(0.3)
        self.line(MARGIN, self.get_y(), self.w - MARGIN, self.get_y())
        self.set_font('Helvetica', '', 8)
        self.set_text_color(*MID)
        self.cell(0, 8,
                  f'Fusion Flow -- Demo Script   |   Page {self.page_no()}',
                  align='C')

    # ── table helpers ─────────────────────────────────────────────────────────

    def _flush_table(self):
        if not self._table_rows:
            self._in_table   = False
            return
        cols  = self._table_cols or self._table_rows[0]
        n     = len(cols)
        if n == 0:
            self._in_table = False
            return
        usable = self.w - self.l_margin - self.r_margin
        col_w  = usable / n

        self.ln(2)
        self.set_fill_color(*TABLE_HEAD)
        self.set_text_color(*DARK)
        self.set_font('Helvetica', 'B', 8.5)
        for h in cols:
            self.cell(col_w, 7, h.strip(), border='B', fill=True, align='L')
        self.ln()

        self.set_font('Helvetica', '', 8.5)
        for ri, row in enumerate(self._table_rows):
            bg = LIGHT_GREY if ri % 2 == 0 else (255, 255, 255)
            self.set_fill_color(*bg)
            for ci, cell in enumerate(row[:n]):
                safe = cell.strip()
                self.multi_cell(
                    col_w, 5.5, safe, border=0, fill=True, align='L',
                    new_x='RIGHT' if ci < n - 1 else 'LMARGIN',
                    new_y='TOP'   if ci < n - 1 else 'NEXT',
                    max_line_height=5.5,
                )
                if ci < n - 1:
                    pass  # fpdf2 auto-restores x after multi_cell RIGHT
            self.set_draw_color(*RULE_GREY)
            self.set_line_width(0.1)
            self.line(self.l_margin, self.get_y(),
                      self.w - self.r_margin, self.get_y())

        self.ln(3)
        self._in_table   = False
        self._table_cols = []
        self._table_rows = []

    # ── core line renderer ────────────────────────────────────────────────────

    def render_lines(self, lines):
        i = 0
        while i < len(lines):
            line = lines[i]

            # table
            if '|' in line and line.strip().startswith('|'):
                cells = [c.strip() for c in line.strip().strip('|').split('|')]
                if all(re.match(r'^[-:]+$', c) for c in cells if c):
                    i += 1
                    continue
                if self._in_table:
                    self._table_rows.append(cells)
                else:
                    self._in_table   = True
                    self._table_cols = cells
                    self._table_rows = []
                i += 1
                continue
            elif self._in_table:
                self._flush_table()

            # blank
            if not line.strip():
                self.ln(2)
                i += 1
                continue

            # horizontal rule
            if re.match(r'^---+$', line.strip()):
                self.set_draw_color(*RULE_GREY)
                self.set_line_width(0.3)
                self.ln(1)
                self.line(MARGIN, self.get_y(), self.w - MARGIN, self.get_y())
                self.ln(3)
                i += 1
                continue

            # H1
            if line.startswith('# ') and not line.startswith('## '):
                self.ln(2)
                self.set_font('Helvetica', 'B', 18)
                self.set_text_color(*BRAND_RED)
                self.multi_cell(0, 9, line[2:].strip(),
                                new_x='LMARGIN', new_y='NEXT')
                self.set_draw_color(*BRAND_RED)
                self.set_line_width(0.6)
                self.line(MARGIN, self.get_y(), self.w - MARGIN, self.get_y())
                self.ln(3)
                i += 1
                continue

            # H2
            if line.startswith('## ') and not line.startswith('### '):
                self.ln(4)
                self.set_font('Helvetica', 'B', 13)
                self.set_text_color(*BRAND_RED)
                self.multi_cell(0, 7, line[3:].strip(),
                                new_x='LMARGIN', new_y='NEXT')
                self.set_draw_color(*BRAND_RED)
                self.set_line_width(0.4)
                self.line(MARGIN, self.get_y(),
                          self.w - MARGIN * 3, self.get_y())
                self.ln(2)
                i += 1
                continue

            # H3
            if line.startswith('### '):
                self.ln(2)
                self.set_font('Helvetica', 'B', 10.5)
                self.set_text_color(*MID)
                self.multi_cell(0, 6, line[4:].strip(),
                                new_x='LMARGIN', new_y='NEXT')
                self.ln(1)
                i += 1
                continue

            # blockquote
            if line.startswith('> '):
                text = line[2:].strip()
                while i + 1 < len(lines) and lines[i + 1].startswith('> '):
                    i += 1
                    text += ' ' + lines[i][2:].strip()
                self.ln(1)
                y0 = self.get_y()
                self.set_font('Helvetica', 'I', 9.5)
                self.set_text_color(*MID)
                clean = re.sub(r'`(.+?)`', r'"\1"',
                        re.sub(r'\*\*(.+?)\*\*', r'\1', text))
                self.set_x(MARGIN + 5)
                self.multi_cell(self.w - self.l_margin - self.r_margin - 5,
                                5.5, clean, new_x='LMARGIN', new_y='NEXT')
                self.set_draw_color(*TEAL)
                self.set_line_width(0.8)
                self.line(MARGIN, y0, MARGIN, self.get_y())
                self.ln(1)
                i += 1
                continue

            # code block
            if line.startswith('```'):
                i += 1
                code_lines = []
                while i < len(lines) and not lines[i].startswith('```'):
                    code_lines.append(lines[i])
                    i += 1
                self.ln(1)
                self.set_fill_color(*LIGHT_GREY)
                self.set_font('Courier', '', 8)
                self.set_text_color(*DARK)
                self.multi_cell(self.w - self.l_margin - self.r_margin,
                                4.8, '\n'.join(code_lines), fill=True,
                                new_x='LMARGIN', new_y='NEXT', border='B')
                self.ln(2)
                i += 1
                continue

            clean = re.sub(r'`(.+?)`', r'\1',
                    re.sub(r'\*\*(.+?)\*\*', r'\1',
                    re.sub(r'\*(.+?)\*',     r'\1', line)))

            # numbered list
            m = re.match(r'^(\d+)\.\s+(.+)', line)
            if m:
                num  = m.group(1)
                text = re.sub(r'`(.+?)`', r'\1',
                       re.sub(r'\*\*(.+?)\*\*', r'\1', m.group(2)))
                self.set_font('Helvetica', '', 9.5)
                self.set_text_color(*DARK)
                self.set_x(MARGIN + 4)
                self.multi_cell(self.w - self.l_margin - self.r_margin - 4,
                                5.5, f'{num}.  {text}',
                                new_x='LMARGIN', new_y='NEXT')
                i += 1
                continue

            # bullet
            if re.match(r'^[-*]\s+', line):
                text = re.sub(r'^[-*]\s+', '', line)
                text = re.sub(r'`(.+?)`', r'\1',
                       re.sub(r'\*\*(.+?)\*\*', r'\1', text))
                self.set_font('Helvetica', '', 9.5)
                self.set_text_color(*DARK)
                self.set_x(MARGIN + 4)
                self.multi_cell(self.w - self.l_margin - self.r_margin - 4,
                                5.5, f'*  {text}',
                                new_x='LMARGIN', new_y='NEXT')
                i += 1
                continue

            # bold label
            if line.startswith('**'):
                self.set_font('Helvetica', 'B', 9.5)
                self.set_text_color(*DARK)
                safe = re.sub(r'\*\*(.+?)\*\*', r'\1',
                       re.sub(r'`(.+?)`', r'\1', line))
                self.multi_cell(0, 5.5, safe,
                                new_x='LMARGIN', new_y='NEXT')
                i += 1
                continue

            # normal paragraph
            self.set_font('Helvetica', '', 9.5)
            self.set_text_color(*DARK)
            if clean.strip():
                self.multi_cell(0, 5.5, clean,
                                new_x='LMARGIN', new_y='NEXT')
            i += 1

        if self._in_table:
            self._flush_table()

    # ── page-break-aware scene renderer ──────────────────────────────────────

    def _fits_on_current_page(self, lines):
        """
        Dry-run: deepcopy self, render lines, check whether we stayed on the
        same page.  Returns True if the whole block fits without a page break.
        """
        probe = copy.deepcopy(self)
        start_page = probe.page
        probe.render_lines(lines)
        return probe.page == start_page

    def render_blocks(self, blocks):
        """
        Render (is_scene, lines) blocks.
        Scene blocks that don't fit on the current page get a page break first.
        Short consecutive scenes that both fit are kept together.
        """
        for is_scene, lines in blocks:
            if is_scene and not self._fits_on_current_page(lines):
                # Only break if we're not already near the top of a fresh page
                page_top = self.t_margin + 5   # 5 mm grace after header
                if self.get_y() > page_top:
                    self.add_page()
            self.render_lines(lines)


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    with open(MD_PATH, encoding='utf-8') as f:
        raw = f.read()

    md     = _sanitize(raw)
    lines  = md.splitlines()
    blocks = _split_into_blocks(lines)

    pdf = DemoPDF()
    pdf.render_blocks(blocks)
    pdf.output(OUT_PATH)
    print(f'PDF saved: {OUT_PATH}')


if __name__ == '__main__':
    main()
