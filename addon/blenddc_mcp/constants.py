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
RACK_RAIL_THICK_MM    = 2.0    # rail stock thickness (14–16 gauge steel)
RACK_RAIL_THICK_M     = 0.002
RACK_RAIL_FLANGE_W_MM = 57.15  # mounting face width (X) — EIA 2.25" spec
RACK_RAIL_FLANGE_W_M  = 0.05715

# Inner clear opening between mounting face inner edges
# Equipment body (446 mm) slides through with ~4 mm clearance each side
RACK_INNER_CLEAR_MM   = 450.0
RACK_INNER_CLEAR_M    = 0.450

# EIA-310: mounting hole centres are 7.938 mm (5/16") inset from equipment panel edge
# Equipment panel = 482.6 mm (19") → hole centres at ±(241.3 − 7.938) = ±233.4 mm
EIA_HOLE_INSET_MM     = 7.938
EIA_HOLE_INSET_M      = 0.007938

# 19" equipment body width (panel minus ears) — server body slides inside rack
EIA_EQUIPMENT_BODY_MM = 446.0
EIA_EQUIPMENT_BODY_M  = 0.446

# Rail setback — distance from door plane to the flange's mounting face
# Front: 75 mm gives clearance for front door swing and cable dressing
# Rear:  125 mm gives clearance for rear door + rear cable management
# Combined usable mounting depth (1000 mm rack): 1000 - 75 - 125 = 800 mm
RACK_SETBACK_FRONT_MM = 75.0
RACK_SETBACK_FRONT_M  = 0.075
RACK_SETBACK_REAR_MM  = 125.0
RACK_SETBACK_REAR_M   = 0.125

# Ear shoulder setback — server is placed 2.5 mm deeper than the rail front face
# so the ear rests ON the flange with a subtle shoulder rather than sitting flush.
EAR_SETBACK_MM = 2.5
EAR_SETBACK_M  = 0.0025

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

# ── Raised floor system ───────────────────────────────────────────────────
# Pedestals: 150×150mm base plate, 50mm square shaft, 100×100mm head plate
# Tile surface is at Z=0 (finished floor); all raised floor geometry is at Z < 0
RF_PEDESTAL_BASE_W_M  = 0.150   # 150 mm base plate width / depth
RF_PEDESTAL_BASE_H_M  = 0.006   # 6 mm base plate thickness
RF_PEDESTAL_SHAFT_W_M = 0.050   # 50 mm square shaft cross-section
RF_PEDESTAL_SHAFT_H_M = 0.438   # shaft height = 450 - 6 (base) - 6 (head) = 438 mm
RF_PEDESTAL_HEAD_W_M  = 0.100   # 100 mm head plate width / depth
RF_PEDESTAL_HEAD_H_M  = 0.006   # 6 mm head plate thickness
RF_PEDESTAL_TOTAL_H_M = 0.450   # total pedestal assembly height (plenum)
RF_GRID_M             = 0.600   # 600 mm grid module (pedestal spacing)
RF_STRINGER_W_M       = 0.025   # 25 mm stringer cross-section width
RF_STRINGER_H_M       = 0.025   # 25 mm stringer cross-section height
RF_TILE_W_M           = 0.600   # 600 mm tile width
RF_TILE_D_M           = 0.600   # 600 mm tile depth
RF_TILE_H_M           = 0.025   # 25 mm tile thickness
RF_TILE_GROUT_M       = 0.004   # 4 mm grout gap between tiles

# ── Fan tray (top exhaust, 1U section at top of rail zone) ────────────────
FAN_TRAY_HEIGHT_M     = RACK_U_M   # fan tray is exactly 1U (44.45 mm) tall
FAN_TRAY_PANEL_H_M    = 0.002      # 2 mm tray plate thickness

# Fan zone footprint (2×2 array of 120 mm fans, 6 mm frame between each)
FAN_SIZE_M        = 0.120   # 120 mm per fan
FAN_FRAME_WALL_M  = 0.006   # 6 mm frame wall between fans
FAN_GRID_COLS     = 2
FAN_GRID_ROWS     = 2
# Derived zone size: 2*120 + 1*6 = 246 mm square, centred in rack width/depth

# ── Vent slot geometry (fan tray intake + top cap exhaust) ────────────────
# Slots confined to the 2×2 fan zone only; surrounding plate stays solid
VENT_BAR_W_M    = 0.005   # 5 mm solid bar between slot openings
VENT_SLOT_GAP_M = 0.020   # 20 mm open slot

# ── Floor mounting L-brackets (bolt-to-floor anchors, replaces casters) ───
# Vertical plate against outer post face; horizontal flange on floor
FLOOR_BRACKET_VERT_H_M   = 0.080   # 80 mm tall vertical plate
FLOOR_BRACKET_VERT_W_M   = 0.040   # 40 mm wide (spans post outer face)
FLOOR_BRACKET_VERT_T_M   = 0.005   # 5 mm sheet metal thickness
FLOOR_BRACKET_FLANGE_L_M = 0.060   # 60 mm floor flange length (outward from post)
FLOOR_BRACKET_FLANGE_T_M = 0.005   # 5 mm flange thickness

# ── Trapeze cable tray hangers ────────────────────────────────────────────
# Ceiling anchor plate → M8 threaded rod → trapeze bar cradling tray bottom
TRAPEZE_CEILING_PLATE_W_M = 0.060   # 60 mm square ceiling anchor plate
TRAPEZE_CEILING_PLATE_T_M = 0.008   # 8 mm thick
TRAPEZE_ROD_DIAM_M        = 0.008   # 8 mm all-thread rod (modelled as square box)
TRAPEZE_BAR_H_M           = 0.030   # 30 mm tall trapeze bar
TRAPEZE_BAR_T_M           = 0.004   # 4 mm thick trapeze bar stock
TRAPEZE_BAR_OVERHANG_M    = 0.040   # bar extends 40 mm past each tray wall

# ── Structural crossbars ──────────────────────────────────────────────────
RACK_CROSSBAR_H_M = 0.030   # 30 mm tall horizontal structural crossbar
RACK_CROSSBAR_T_M = 0.004   # 4 mm thick

# ── Quality tiers — single source of truth ────────────────────────────────
# Each tier maps to a flat dict of feature flags consumed by create_rack_cabinet,
# create_server_chassis, create_network_switch, create_patch_panel,
# create_raised_floor, and export_rack_collection_ue5.
#
# Tiers in descending detail order:
#   ultra  → hero-quality, maximum geometry, no compromises
#   high   → default; detailed but not hero (current historical behaviour)
#   medium → balanced; suitable for mid-distance rendering
#   low    → performance; box proxies + single-mesh floor for far-LOD / mobile
QUALITY_TIERS: dict = {
    "ultra": {
        # Rack rails
        "eia_holes":         True,   # full EIA-310 punched through-holes
        "lod_rails":         True,   # also emit _Rails_LOD1 solid variant
        "rack_rails":        True,   # include rail flanges at all
        # Rack structural
        "fan_tray":          True,
        "crossbars":         True,
        # Server / switch bezel & detail
        "bezel":             True,   # top/bottom/right bezel frame strips
        "server_bays":       True,   # drive bay surrounds on server bezel
        "bay_3d":            True,   # deep 8 mm bay housing recesses (ultra only)
        "vents":             True,   # side ventilation louvre strips
        "grille":            True,   # rear exhaust tile grille
        "ear_screws":        True,   # visible screw head bumps on mounting ears
        # Floor
        "floor_perforated":  True,   # waffle-grid perforated tiles in cold aisles
        "floor_single_mesh": False,  # collapse entire floor to one flat mesh
        # Export
        "lod_level":         0,
    },
    "high": {
        "eia_holes":         True,
        "lod_rails":         True,
        "rack_rails":        True,
        "fan_tray":          True,
        "crossbars":         True,
        "bezel":             True,
        "server_bays":       True,
        "bay_3d":            False,  # flat bay faces (no deep housing)
        "vents":             True,
        "grille":            True,
        "ear_screws":        False,
        "floor_perforated":  True,
        "floor_single_mesh": False,
        "lod_level":         1,
    },
    "medium": {
        "eia_holes":         False,  # solid rail flanges
        "lod_rails":         False,
        "rack_rails":        True,
        "fan_tray":          True,
        "crossbars":         True,
        "bezel":             True,
        "server_bays":       True,   # basic flat bays, no deep recesses
        "bay_3d":            False,
        "vents":             False,
        "grille":            False,
        "ear_screws":        False,
        "floor_perforated":  False,  # all tiles solid (bake-ready)
        "floor_single_mesh": False,
        "lod_level":         2,
    },
    "low": {
        "eia_holes":         False,
        "lod_rails":         False,
        "rack_rails":        False,  # no mounting rails at all
        "fan_tray":          False,
        "crossbars":         False,
        "bezel":             False,
        "server_bays":       False,
        "bay_3d":            False,
        "vents":             False,
        "grille":            False,
        "ear_screws":        False,
        "floor_perforated":  False,
        "floor_single_mesh": True,   # single flat slab + grid normal map hint
        "lod_level":         3,
    },
}
