"""
EIA-310 rack dimensional constants and UE5 export settings.

All measurements in both millimetres (_MM suffix) and metres (_M suffix).
Metres are used in Blender (1 Blender unit = 1 metre).
"""

# ── EIA-310 rack unit ──────────────────────────────────────────────────────
RACK_U_MM = 44.45        # one rack unit in mm (1¾ inch)
RACK_U_M  = 0.04445      # one rack unit in metres

# ── EIA-310 rail geometry ──────────────────────────────────────────────────
EIA_RAIL_SPAN_MM = 482.6   # inner face to inner face, 19" standard
EIA_RAIL_SPAN_M  = 0.4826

# Three hole positions per U (bottom of U = 0, offsets along U height)
RACK_HOLE_OFFSETS_MM = (0.0, 15.88, 28.57)
RACK_HOLE_SIZE_MM = 9.525  # M6 square hole
RACK_HOLE_SIZE_M  = 0.009525

# ── Cabinet defaults ───────────────────────────────────────────────────────
RACK_DEFAULT_U_HEIGHT   = 42
RACK_DEFAULT_WIDTH_MM   = 600.0
RACK_DEFAULT_DEPTH_MM   = 1000.0

# Interior rail zone (42 × 44.45 mm = 1866.9 mm)
RACK_INTERIOR_HEIGHT_MM = 1866.9
RACK_INTERIOR_HEIGHT_M  = 1.8669

# Relative Z positions for door hinges along the rail height (bottom → top)
HINGE_POSITIONS = [0.10, 0.50, 0.90]

# ── Structural members ─────────────────────────────────────────────────────
# Base pedestal (cable management / levelling feet space)
RACK_BASE_HEIGHT_MM = 60.0
RACK_BASE_HEIGHT_M  = 0.060

# Top cap / cable tray space
RACK_TOP_HEIGHT_MM = 73.1
RACK_TOP_HEIGHT_M  = 0.0731

# 4-post corner extrusion (square cross-section)
RACK_POST_SIZE_MM = 60.0
RACK_POST_SIZE_M  = 0.060

# Side / top / rear panel sheet metal thickness
RACK_SHEET_THICK_MM = 1.5
RACK_SHEET_THICK_M  = 0.0015

# ── L-bracket mounting rail ────────────────────────────────────────────────
RACK_RAIL_THICK_MM  = 3.0   # rail stock thickness
RACK_RAIL_THICK_M   = 0.003
RACK_RAIL_FLANGE_MM = 20.0  # depth of horizontal flange (where equipment screws attach)
RACK_RAIL_FLANGE_M  = 0.020

# ── Door hardware ──────────────────────────────────────────────────────────
HINGE_PIN_DIAM_M   = 0.008  # 8 mm diameter hinge pin
HINGE_PIN_HEIGHT_M = 0.020  # 20 mm tall pin stub geometry
HINGE_COUNT_PER_DOOR = 3    # top / middle / bottom

LATCH_WIDTH_M  = 0.025   # 25 mm latch body
LATCH_HEIGHT_M = 0.015
LATCH_DEPTH_M  = 0.008

# Inset from rack corner to hinge / latch centre
ANCHOR_INSET_M = 0.030

# ── UE5 export settings ────────────────────────────────────────────────────
UE5_AXIS_FORWARD   = '-X'
UE5_AXIS_UP        = 'Z'
UE5_SCALE_OPTIONS  = 'FBX_SCALE_ALL'
UE5_MESH_SMOOTH    = 'FACE'

UCX_PREFIX    = 'UCX_'     # UE5 automatic collision mesh prefix
SOCKET_PREFIX = 'SOCKET_'  # UE5 socket attachment point prefix

# ── Door geometry ──────────────────────────────────────────────────────────
# Front/rear door panel (sheet metal, no frame — hinges on left, latch right)
DOOR_SHEET_THICK_M = 0.002    # 2 mm door skin (slightly thicker than panels)

# Vent slot pattern defaults (used by add_door_vent_pattern GN tool)
DOOR_VENT_SLOT_W_M  = 0.010   # 10 mm slot width
DOOR_VENT_SLOT_H_M  = 0.050   # 50 mm slot height
DOOR_VENT_GAP_X_M   = 0.012   # horizontal gap between slots
DOOR_VENT_GAP_Y_M   = 0.008   # vertical gap between slots
DOOR_VENT_MARGIN_M  = 0.040   # edge margin (no slots within 40 mm of edge)

# ── Cable management ───────────────────────────────────────────────────────
BRUSH_STRIP_HEIGHT_M     = RACK_U_M       # 1U tall (44.45 mm)
BRUSH_STRIP_DEPTH_M      = 0.050          # 50 mm stub depth
CABLE_ENTRY_CUTOUT_W_M   = 0.100          # default cutout width (100 mm)
CABLE_ENTRY_CUTOUT_H_M   = RACK_U_M       # default cutout height (1U)
CABLE_TRAY_DEPTH_M       = 0.060          # top tray channel depth (Z)
CABLE_TRAY_WALL_THICK_M  = 0.002          # tray side wall thickness
VERT_CABLE_MGMT_WIDTH_M  = 0.050          # 50 mm vertical channel width

# ── LOD defaults ───────────────────────────────────────────────────────────
LOD1_DEFAULT_RATIO = 0.40   # LOD1 target: 40 % of LOD0 triangles
LOD2_DEFAULT_RATIO = 0.15   # LOD2 target: 15 % of LOD0 triangles
