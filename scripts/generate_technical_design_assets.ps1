param(
    [string]$OutputDir = "\\pl-az-sdf-plint\Fusion_Production\Scratch\Fusion_Flow_V3_QAS\Documentation_Layer\assets\technical_overview"
)

Add-Type -AssemblyName System.Drawing

function New-Brush($hex) {
    return New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml($hex))
}

function New-Pen($hex, $width = 2) {
    return New-Object System.Drawing.Pen ([System.Drawing.ColorTranslator]::FromHtml($hex), $width)
}

function Draw-RoundRect($g, $x, $y, $w, $h, $radius, $fillHex, $strokeHex, $strokeWidth = 2) {
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $radius * 2
    $path.AddArc($x, $y, $d, $d, 180, 90)
    $path.AddArc($x + $w - $d, $y, $d, $d, 270, 90)
    $path.AddArc($x + $w - $d, $y + $h - $d, $d, $d, 0, 90)
    $path.AddArc($x, $y + $h - $d, $d, $d, 90, 90)
    $path.CloseFigure()
    $fill = New-Brush $fillHex
    $pen = New-Pen $strokeHex $strokeWidth
    $g.FillPath($fill, $path)
    $g.DrawPath($pen, $path)
    $fill.Dispose()
    $pen.Dispose()
    $path.Dispose()
}

function Draw-Text($g, $text, $x, $y, $w, $h, $size = 20, $style = "Regular", $color = "#1F2933", $align = "Center") {
    $fontStyle = [System.Drawing.FontStyle]::Regular
    if ($style -eq "Bold") { $fontStyle = [System.Drawing.FontStyle]::Bold }
    if ($style -eq "Italic") { $fontStyle = [System.Drawing.FontStyle]::Italic }
    $font = New-Object System.Drawing.Font("Segoe UI", $size, $fontStyle, [System.Drawing.GraphicsUnit]::Pixel)
    $brush = New-Brush $color
    $format = New-Object System.Drawing.StringFormat
    $format.Alignment = [System.Drawing.StringAlignment]::$align
    $format.LineAlignment = [System.Drawing.StringAlignment]::Center
    $rect = New-Object System.Drawing.RectangleF($x, $y, $w, $h)
    $g.DrawString($text, $font, $brush, $rect, $format)
    $font.Dispose()
    $brush.Dispose()
    $format.Dispose()
}

function Draw-LineArrow($g, $x1, $y1, $x2, $y2, $hex = "#1F4E79", $width = 3) {
    $pen = New-Pen $hex $width
    $cap = New-Object System.Drawing.Drawing2D.AdjustableArrowCap(5, 7, $true)
    $pen.CustomEndCap = $cap
    $g.DrawLine($pen, $x1, $y1, $x2, $y2)
    $cap.Dispose()
    $pen.Dispose()
}

function New-Canvas($path, $width = 1800, $height = 1100) {
    $bitmap = New-Object System.Drawing.Bitmap($width, $height)
    $g = [System.Drawing.Graphics]::FromImage($bitmap)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::ClearTypeGridFit
    $g.Clear([System.Drawing.ColorTranslator]::FromHtml("#F7FAFC"))
    return @{ Bitmap = $bitmap; Graphics = $g; Path = $path }
}

function Save-Canvas($canvas) {
    $canvas.Graphics.Dispose()
    $canvas.Bitmap.Save($canvas.Path, [System.Drawing.Imaging.ImageFormat]::Png)
    $canvas.Bitmap.Dispose()
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

# 1. High-level design.
$canvas = New-Canvas (Join-Path $OutputDir "high_level_design.png") 1800 1100
$g = $canvas.Graphics
Draw-Text $g "Fusion Flow V3 QAS - High Level Design" 60 30 1680 60 34 "Bold" "#1F4E79"
Draw-Text $g "Architecture proposal aligned with Flow V2 BKD production patterns" 60 88 1680 38 20 "Regular" "#5B6778"

Draw-RoundRect $g 70 190 300 170 18 "#FFFFFF" "#B8C6D9" 2
Draw-Text $g "Inbound Sources" 90 210 260 34 22 "Bold" "#1F4E79"
Draw-Text $g "Microsoft Graph mailbox`nExcel / CSV files`nTenant routes" 95 250 250 92 19 "Regular" "#1F2933"

Draw-RoundRect $g 500 180 350 190 18 "#EAF3FB" "#1F4E79" 3
Draw-Text $g "Integration Layer" 520 198 310 34 22 "Bold" "#1F4E79"
Draw-Text $g "FLOW_V3 Step 01`nGraph ingestion`nPack generation`nValidation intake" 525 238 300 110 19 "Regular" "#1F2933"

Draw-RoundRect $g 1010 160 700 230 20 "#FFFFFF" "#B8C6D9" 2
Draw-Text $g "Azure SQL / SQL Server Data Model" 1035 180 650 34 22 "Bold" "#1F4E79"
$schemas = @(
    @("CFG", "Configuration and tenant rules", "#D9EAF7"),
    @("EXC", "Execution runs and technical outcomes", "#E8F5E9"),
    @("ING", "Immutable inbound source trace", "#FFF4DE"),
    @("STG", "Working business state", "#F3E8FF"),
    @("TSS", "Official API mirror and responses", "#E0F2FE")
)
$sx = 1040
foreach ($schema in $schemas) {
    Draw-RoundRect $g $sx 240 122 92 12 $schema[2] "#9FB3C8" 1
    Draw-Text $g $schema[0] ($sx+8) 250 106 24 22 "Bold" "#243B53"
    Draw-Text $g $schema[1] ($sx+8) 278 106 42 13 "Regular" "#334E68"
    $sx += 130
}

Draw-RoundRect $g 500 565 350 210 18 "#FFFFFF" "#B8C6D9" 2
Draw-Text $g "FLOW_V3 Jobs" 525 585 300 34 22 "Bold" "#1F4E79"
Draw-Text $g "02 ENS submit`n03 Cargo submit`n04 Status watcher`n05 SDI autosubmit" 530 625 290 112 19 "Regular" "#1F2933"

Draw-RoundRect $g 1010 555 330 220 18 "#EAF3FB" "#1F4E79" 3
Draw-Text $g "Flask Support Portal" 1035 575 280 34 22 "Bold" "#1F4E79"
Draw-Text $g "Jinja templates`nNative HTML/CSS/JS`nQueues, logs, settings`nOperational analytics" 1040 615 270 118 19 "Regular" "#1F2933"

Draw-RoundRect $g 1380 555 330 220 18 "#FFFFFF" "#B8C6D9" 2
Draw-Text $g "External Services" 1405 575 280 34 22 "Bold" "#1F4E79"
Draw-Text $g "TSS API`nMicrosoft Graph`nSMTP / notifications`nFuture Azure monitoring" 1410 615 270 118 19 "Regular" "#1F2933"

Draw-LineArrow $g 370 275 500 275
Draw-LineArrow $g 850 275 1010 275
Draw-LineArrow $g 1180 390 1180 555
Draw-LineArrow $g 850 670 1010 670
Draw-LineArrow $g 1340 665 1380 665
Draw-LineArrow $g 675 370 675 565
Draw-LineArrow $g 1180 555 1180 390 "#64748B" 2

Draw-Text $g "Design intent: simple runtime, auditable data layers, support-first operation, and Azure-ready deployment." 120 960 1560 50 22 "Bold" "#243B53"
Save-Canvas $canvas

# 2. Technology stack.
$canvas = New-Canvas (Join-Path $OutputDir "technology_stack.png") 1800 1050
$g = $canvas.Graphics
Draw-Text $g "Recommended Technology Stack" 60 35 1680 60 34 "Bold" "#1F4E79"
Draw-Text $g "Proposed baseline for team approval before implementation" 60 92 1680 38 20 "Regular" "#5B6778"

$layers = @(
    @("Frontend", "Jinja templates + native HTML/CSS/JS", "No SPA build pipeline in phase 1; fast operational screens and low maintenance.", "#E0F2FE"),
    @("Backend", "Python 3.12 + Flask + Blueprints", "Matches V2 production patterns and current V3 placeholder app.", "#E8F5E9"),
    @("Automation", "FLOW_V3 scripts + scheduled jobs", "Clear 01-05 execution model; easier to debug before heavier orchestration.", "#FFF4DE"),
    @("Data", "Azure SQL / SQL Server + pyodbc + ODBC Driver 18", "Structured CFG/EXC/ING/STG/TSS model with auditable traceability.", "#F3E8FF"),
    @("Infrastructure", "Azure target; Docker/Render optional for QAS/demo", "Azure aligns with Graph, Key Vault, App Insights and storage.", "#FCE7F3")
)
$y = 175
foreach ($layer in $layers) {
    Draw-RoundRect $g 170 $y 1460 125 22 $layer[3] "#9FB3C8" 2
    Draw-Text $g $layer[0] 210 ($y+20) 260 34 25 "Bold" "#1F4E79" "Near"
    Draw-Text $g $layer[1] 510 ($y+18) 1040 34 24 "Bold" "#243B53" "Near"
    Draw-Text $g $layer[2] 510 ($y+58) 1040 42 19 "Regular" "#334E68" "Near"
    $y += 145
}
Draw-RoundRect $g 260 910 1280 70 18 "#FFFFFF" "#1F4E79" 2
Draw-Text $g "Commercial rationale: reuse what already works, reduce dependency surface, improve supportability, and defer heavy framework cost until the product needs it." 300 922 1200 46 22 "Bold" "#243B53"
Save-Canvas $canvas

# 3. Runtime and support model.
$canvas = New-Canvas (Join-Path $OutputDir "runtime_support_model.png") 1800 1050
$g = $canvas.Graphics
Draw-Text $g "Runtime and Support Model" 60 35 1680 60 34 "Bold" "#1F4E79"
Draw-Text $g "Support-first architecture for automated customs workflows" 60 92 1680 38 20 "Regular" "#5B6778"

$cols = @(
    @("Runtime", "Flask portal`nGunicorn/WSGI`nScheduled FLOW_V3 jobs`nHealth endpoint", 90),
    @("Trace", "EXC execution runs`nING source files/messages`nSTG working state`nTSS response mirror", 520),
    @("Support", "Technical logs`nIngestion queue`nRetry/cancel controls`nFailure explanations", 950),
    @("Governance", "CFG runtime gates`nTenant settings`nKey Vault / env secrets`nDeployment checks", 1380)
)
foreach ($col in $cols) {
    Draw-RoundRect $g $col[2] 215 330 520 22 "#FFFFFF" "#B8C6D9" 2
    Draw-Text $g $col[0] ($col[2]+25) 245 280 38 25 "Bold" "#1F4E79"
    Draw-Text $g $col[1] ($col[2]+35) 335 260 220 21 "Regular" "#1F2933"
}
Draw-LineArrow $g 420 475 520 475
Draw-LineArrow $g 850 475 950 475
Draw-LineArrow $g 1280 475 1380 475
Draw-LineArrow $g 1545 735 1545 825 "#64748B" 3
Draw-LineArrow $g 1545 825 255 825 "#64748B" 3
Draw-LineArrow $g 255 825 255 735 "#64748B" 3
Draw-Text $g "Feedback loop: support evidence and incidents should become configuration, validation or deployment improvements." 330 850 1140 54 23 "Bold" "#243B53"
Draw-RoundRect $g 270 930 1260 55 16 "#EAF3FB" "#1F4E79" 2
Draw-Text $g "Phase 1 analytics should measure operations: volume, failures, latency, blocked jobs and retry outcomes." 305 938 1190 38 21 "Bold" "#1F4E79"
Save-Canvas $canvas

Write-Host "Generated technical overview assets in $OutputDir"
