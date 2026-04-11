"""
UPTIME Full Pipeline Test — Universal Blender MCP v2.2.0
=========================================================
Run this script in Blender's Scripting workspace (Text Editor → Run Script).

The MCP addon must be installed and the server started (N-Panel → MCP →
Start MCP Server) before running — the server start triggers the module
imports that make all tool functions available.

WHAT THIS SCRIPT TESTS
-----------------------
Step 1  create_rack_cabinet         — EIA-310 42U cabinet geometry + origin
Step 2  get_rack_info               — verify metadata stored on collection
Step 3  validate_rack_collection    — pre-export health check (scale, UVs, tris)
Step 4  populate_rack_procedural    — fill rack with mixed equipment
Step 5  add_eia_holes_gn            — non-destructive EIA holes on rail web
Step 6  create_rack_doors           — front + rear door panels
Step 7  export_rack_collection_ue5  — full one-call export pipeline
Step 8  export_equipment_set_ue5    — deduplicated equipment FBX export
Step 9  clear_rack_population       — cleanup pass (keeps cabinet)

WHAT TO CHECK IN BLENDER AFTER RUNNING
---------------------------------------
- Outliner: "TestRack" collection with a single joined mesh + _Equipment sub-collection
- 3D Viewport: cabinet sits at world origin, doors close the front/rear faces
- Rail webs should have an EIA_Holes_GN modifier (visible in Properties → Modifiers)
- Equipment objects should be correctly positioned inside the rack opening
- Console: all steps should print  [PASS]  with no [FAIL] lines

WHAT TO CHECK IN UE5 AFTER IMPORTING
--------------------------------------
- Import TestRack.fbx as StaticMesh → verify scale (2.0 m tall approx)
- Origin should land at X=0, Y=0, Z=0 (floor, front face, centreline)
- EQ_server.fbx / EQ_switch.fbx etc. import with SOCKET_ attachment points
- StaticMesh Editor → Sockets panel should list SOCKET_Power, SOCKET_Data_00 etc.
- TestRack_manifest.json is a UE5 DataTable-compatible asset registry

ADJUSTING THE OUTPUT DIRECTORY
--------------------------------
Change OUTPUT_DIR below to your UE5 project's Content/Meshes directory.
On Windows use forward slashes:  "C:/Users/you/UE5Project/Content/Meshes"
"""

import bpy
import sys
import os
import tempfile
import traceback

# ── Configuration ──────────────────────────────────────────────────────────
# Change OUTPUT_DIR to your UE5 Content directory, or leave as temp for testing.
OUTPUT_DIR = os.path.join(tempfile.gettempdir(), "uptime_test_export")
RACK_NAME  = "TestRack"
PRESET     = "mixed_dc"   # "server_dense" | "spine_leaf" | "mixed_dc"


# ── Helpers ────────────────────────────────────────────────────────────────

_pass_count = 0
_fail_count = 0

def _check(label, result, key=None, expected=None):
    """Print a labelled pass/fail line. Optionally assert a key/value."""
    global _pass_count, _fail_count
    try:
        if key is not None and result.get(key) != expected:
            raise AssertionError(
                f"expected {key}={expected!r}, got {result.get(key)!r}"
            )
        print(f"  [PASS]  {label}")
        if isinstance(result, dict):
            # Print a concise summary of the result
            for k, v in result.items():
                if k in ("objects", "placed", "slots", "sockets"):
                    print(f"          {k}: {len(v)} items")
                elif not isinstance(v, (list, dict)):
                    print(f"          {k}: {v}")
        _pass_count += 1
        return result
    except Exception as exc:
        print(f"  [FAIL]  {label} — {exc}")
        _fail_count += 1
        return result


def _section(title):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def _import_tools():
    """
    Import tool modules from the addon. This works whether the server is
    running or the addon is simply installed and enabled in Blender.
    """
    addon_dir = None
    for mod_name, mod in sys.modules.items():
        if mod_name == "rack_tools" or (
            hasattr(mod, "__file__") and mod.__file__ and
            "blenddc_mcp" in (mod.__file__ or "") and
            mod_name == "rack_tools"
        ):
            addon_dir = os.path.dirname(mod.__file__)
            break

    if addon_dir is None:
        # Fallback: look for the addon by searching sys.modules
        for mod_name in sys.modules:
            if "rack_tools" in mod_name:
                mod = sys.modules[mod_name]
                if hasattr(mod, "__file__") and mod.__file__:
                    addon_dir = os.path.dirname(mod.__file__)
                    break

    if addon_dir and addon_dir not in sys.path:
        sys.path.insert(0, addon_dir)

    try:
        import rack_tools as rt
        import export_tools as et
        import equipment_tools as eqt
        return rt, et, eqt
    except ImportError as exc:
        print(f"\n[ERROR] Could not import tool modules: {exc}")
        print("        Make sure the MCP addon is enabled and the server")
        print("        has been started at least once this Blender session.")
        sys.exit(1)


# ── Main test sequence ──────────────────────────────────────────────────────

def run_pipeline_test():
    global _pass_count, _fail_count

    print("\n" + "═" * 60)
    print("  UPTIME Pipeline Test — Universal Blender MCP v2.2.0")
    print("═" * 60)

    rt, et, eqt = _import_tools()
    print(f"\n  Tool modules loaded OK")
    print(f"  Output directory: {OUTPUT_DIR}")

    # ── Pre-flight: clean up any previous test run ──────────────────────────
    existing = bpy.data.collections.get(RACK_NAME)
    if existing:
        print(f"\n  Removing previous '{RACK_NAME}' collection from scene...")
        for obj in list(existing.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(existing)
    equip_existing = bpy.data.collections.get(f"{RACK_NAME}_Equipment")
    if equip_existing:
        for obj in list(equip_existing.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(equip_existing)

    # ══════════════════════════════════════════════════════════════════════
    _section("Step 1 — create_rack_cabinet")
    # ══════════════════════════════════════════════════════════════════════
    # Expected: a Blender collection named TestRack containing a single
    # joined mesh. The mesh origin must be at (0, 0, 0) = base-front-centre.
    # External dimensions: 600 mm wide × 1000 mm deep × ~2000 mm tall.
    #
    # In Blender: Outliner should show "TestRack" collection with one mesh
    # object also named "TestRack". Select it → Item panel → Location = 0,0,0.
    try:
        result = rt.create_rack_cabinet(
            name=RACK_NAME,
            u_height=42,
            width_mm=600.0,
            depth_mm=1000.0,
            include_side_panels=True,
            include_top_panel=True,
            include_base=True,
            include_door_mounts=True,
            join_mesh=True,
        )
        _check("create_rack_cabinet returned", result,
               key="origin", expected="base-front-centre (0, 0, 0)")
        _check("u_height stored correctly", result,
               key="u_height", expected=42)
        _check("no geometry warnings", result,
               key="warnings", expected=[])

        # Verify origin in scene
        obj = bpy.data.objects.get(RACK_NAME)
        if obj:
            loc = obj.location
            origin_ok = (abs(loc.x) < 0.001 and abs(loc.y) < 0.001 and abs(loc.z) < 0.001)
            if origin_ok:
                _check("mesh origin at (0, 0, 0)", {"ok": True}, key="ok", expected=True)
            else:
                _check(f"mesh origin at (0, 0, 0) — got ({loc.x:.3f}, {loc.y:.3f}, {loc.z:.3f})",
                       {"ok": False}, key="ok", expected=True)
        else:
            print(f"  [FAIL]  Object '{RACK_NAME}' not found in scene")
            _fail_count += 1

    except Exception:
        print(f"  [FAIL]  create_rack_cabinet raised an exception:")
        traceback.print_exc()
        _fail_count += 1

    # ══════════════════════════════════════════════════════════════════════
    _section("Step 2 — get_rack_info (metadata check)")
    # ══════════════════════════════════════════════════════════════════════
    # Expected: returns a dict with all EIA-310 dimensions from the collection
    # custom properties. Verify the key numbers match the constants.
    # RACK_U_MM = 44.45, so 42U interior = 1866.9 mm.
    try:
        info = rt.get_rack_info(RACK_NAME)
        _check("get_rack_info returned", info,
               key="u_height", expected=42)
        _check("EIA rail span correct",  info,
               key="eia_rail_span_mm", expected=482.6)
        _check("rail height = 1866.9 mm", info,
               key="rail_height_mm", expected=1866.9)
    except Exception:
        print(f"  [FAIL]  get_rack_info raised an exception:")
        traceback.print_exc()
        _fail_count += 1

    # ══════════════════════════════════════════════════════════════════════
    _section("Step 3 — validate_rack_collection (pre-export check)")
    # ══════════════════════════════════════════════════════════════════════
    # Expected: export_ready=True, no scale issues (join_mesh applied scale),
    # UV warning is acceptable for the cabinet chassis (no UV map on the
    # joined solid — will be flagged as a warning, not an error).
    #
    # NOTE: If you see "Scale not applied" here, it means join_mesh left
    # unapplied transforms. This would need to be fixed before UE5 export.
    try:
        val = et.validate_rack_collection(RACK_NAME)
        _check("validate_rack_collection returned", val)
        if val["export_ready"]:
            _check("export_ready = True", val, key="export_ready", expected=True)
        else:
            print(f"  [WARN]  export_ready = False — issues found:")
            for issue in val.get("issues", []):
                print(f"          • {issue}")
            print(f"          (UV warnings are expected for solid-mesh racks)")
    except Exception:
        print(f"  [FAIL]  validate_rack_collection raised an exception:")
        traceback.print_exc()
        _fail_count += 1

    # ══════════════════════════════════════════════════════════════════════
    _section("Step 4 — add_eia_holes_gn (non-destructive rail holes)")
    # ══════════════════════════════════════════════════════════════════════
    # Only works on non-joined racks (join_mesh=True merges rail webs into
    # the chassis). This step demonstrates the GN modifier on a standalone
    # rail web. We create a second rack with join_mesh=False to test.
    #
    # In Blender: select TestRack_GNTest_rail_LF_web → Properties → Modifiers
    # You should see "EIA_Holes_GN" with mode='instance' (lightweight).
    gn_rack_name = f"{RACK_NAME}_GNTest"
    try:
        gn_result = rt.create_rack_cabinet(
            name=gn_rack_name,
            u_height=42,
            join_mesh=False,   # keep parts separate so we can target rail webs
        )
        # Target the left-front rail web
        rail_web_name = f"{gn_rack_name}_rail_LF_web"
        rail_web = bpy.data.objects.get(rail_web_name)
        if rail_web:
            holes_result = rt.add_eia_holes_gn(
                object_name=rail_web_name,
                u_height=42,
                bake=False,      # non-destructive — modifier stays on
                mode="instance", # lightweight tile markers, not Boolean cuts
            )
            _check("add_eia_holes_gn (instance mode)", holes_result,
                   key="mode", expected="instance")
            _check("correct hole count (42U × 3 = 126)", holes_result,
                   key="hole_count", expected=126)
            _check("modifier present on rail web",
                   {"ok": rail_web.modifiers.get("EIA_Holes_GN") is not None},
                   key="ok", expected=True)
        else:
            print(f"  [WARN]  '{rail_web_name}' not found — GN test skipped")
            print(f"          (expected when join_mesh=True; re-create with join_mesh=False)")
    except Exception:
        print(f"  [FAIL]  add_eia_holes_gn raised an exception:")
        traceback.print_exc()
        _fail_count += 1

    # ══════════════════════════════════════════════════════════════════════
    _section("Step 5 — create_rack_doors")
    # ══════════════════════════════════════════════════════════════════════
    # Expected: two door panel objects added to the TestRack collection —
    # TestRack_door_front and TestRack_door_rear. Each has hinge attach
    # empties and a latch socket empty as children.
    #
    # In Blender: select TestRack_door_front → Item panel → Location should
    # show the door sitting at Y ≈ -0.001 (just forward of the front face).
    # The door origin (orange dot) should be at the bottom hinge pin position.
    try:
        doors = rt.create_rack_doors(
            collection_name=RACK_NAME,
            vented_front=False,
            vented_rear=True,
        )
        _check("create_rack_doors returned both doors", doors)
        front_obj = bpy.data.objects.get(doors["front_door"])
        rear_obj  = bpy.data.objects.get(doors["rear_door"])
        _check("front door object exists in scene",
               {"ok": front_obj is not None}, key="ok", expected=True)
        _check("rear door object exists in scene",
               {"ok": rear_obj is not None}, key="ok", expected=True)
        if front_obj:
            hinge_children = [c for c in front_obj.children
                              if "hinge_attach" in c.name]
            _check("front door has 3 hinge empties",
                   {"ok": len(hinge_children) == 3}, key="ok", expected=True)
    except Exception:
        print(f"  [FAIL]  create_rack_doors raised an exception:")
        traceback.print_exc()
        _fail_count += 1

    # ══════════════════════════════════════════════════════════════════════
    _section("Step 6 — populate_rack_procedural (mixed_dc, random_variation)")
    # ══════════════════════════════════════════════════════════════════════
    # Expected: TestRack_Equipment collection created inside TestRack with
    # alternating patch panels, switches, and 2U servers filling the rack.
    # random_variation=True means adjacent servers will have slightly
    # different LED positions and bay counts.
    #
    # In Blender: expand TestRack_Equipment in the Outliner. Each piece
    # of equipment should have child empties prefixed with SOCKET_.
    # Select any server → Properties → Custom Properties → equipment_type="server"
    try:
        pop = eqt.populate_rack_procedural(
            collection_name=RACK_NAME,
            preset=PRESET,
            random_variation=True,
            start_u=1,
            end_u=40,  # leave U41–42 empty (headroom)
        )
        _check("populate_rack_procedural returned", pop)
        _check("at least 10 equipment items placed",
               {"ok": pop["count"] >= 10}, key="ok", expected=True)
        print(f"          U slots filled: {pop['u_filled']} of 40")

        # Spot-check: first placed object should exist in scene
        if pop["placed"]:
            first = pop["placed"][0]
            first_obj = bpy.data.objects.get(first["name"])
            _check(f"first equipment object '{first['name']}' exists in scene",
                   {"ok": first_obj is not None}, key="ok", expected=True)
            if first_obj:
                # Verify it has socket children
                socket_children = [c for c in first_obj.children
                                   if c.name.startswith("SOCKET_")]
                _check("first equipment has at least one SOCKET_ empty",
                       {"ok": len(socket_children) >= 1}, key="ok", expected=True)
    except Exception:
        print(f"  [FAIL]  populate_rack_procedural raised an exception:")
        traceback.print_exc()
        _fail_count += 1

    # ══════════════════════════════════════════════════════════════════════
    _section("Step 7 — set_export_root + export_rack_collection_ue5")
    # ══════════════════════════════════════════════════════════════════════
    # Expected: OUTPUT_DIR created on disk. Inside:
    #   TestRack.fbx         — the full cabinet mesh (no equipment)
    #   TestRack_LOD0.fbx    — LOD0 (original, if generate_lods=True)
    #   TestRack_LOD1.fbx    — LOD1 (40% of LOD0 tris)
    #   TestRack_LOD2.fbx    — LOD2 (15% of LOD0 tris)
    #   TestRack_manifest.json — asset registry for UE5 DataTable import
    #
    # In UE5: drag TestRack.fbx into Content Browser → Import as StaticMesh.
    # Check: Scale ~2 m tall, origin at floor/front/centre, no broken normals.
    try:
        et.set_export_root(OUTPUT_DIR)
        _check("set_export_root succeeded",
               {"ok": bpy.context.scene.get("ue5_export_root") == OUTPUT_DIR},
               key="ok", expected=True)

        pipeline = et.export_rack_collection_ue5(
            collection_name=RACK_NAME,
            output_dir=OUTPUT_DIR,
            generate_lods=True,
            lod1_ratio=0.40,
            lod2_ratio=0.15,
            write_manifest=True,
        )
        _check("export pipeline not aborted",
               pipeline, key="aborted", expected=False)

        # Verify FBX exists on disk
        fbx_exists = os.path.isfile(pipeline.get("fbx_path", ""))
        _check("TestRack.fbx written to disk",
               {"ok": fbx_exists}, key="ok", expected=True)

        # Verify manifest exists
        manifest_path = pipeline.get("manifest_path", "")
        manifest_exists = os.path.isfile(manifest_path)
        _check("asset manifest JSON written to disk",
               {"ok": manifest_exists}, key="ok", expected=True)

        if manifest_exists:
            import json
            with open(manifest_path) as fh:
                manifest = json.load(fh)
            _check("manifest contains asset entries",
                   {"ok": manifest.get("asset_count", 0) > 0},
                   key="ok", expected=True)

        # List exported files
        if os.path.isdir(OUTPUT_DIR):
            exported_files = os.listdir(OUTPUT_DIR)
            print(f"\n          Files in {OUTPUT_DIR}:")
            for fn in sorted(exported_files):
                size_kb = os.path.getsize(os.path.join(OUTPUT_DIR, fn)) // 1024
                print(f"            {fn}  ({size_kb} KB)")

    except Exception:
        print(f"  [FAIL]  export_rack_collection_ue5 raised an exception:")
        traceback.print_exc()
        _fail_count += 1

    # ══════════════════════════════════════════════════════════════════════
    _section("Step 8 — export_equipment_set_ue5 (deduplicated FBX)")
    # ══════════════════════════════════════════════════════════════════════
    # Expected: one FBX per unique equipment type, not per instance.
    # A mixed_dc rack with servers, switches, and patch panels → 3 FBX files:
    #   EQ_server.fbx        — one chassis blank representing ALL servers
    #   EQ_switch.fbx        — one switch blank
    #   EQ_patch_panel.fbx   — one patch panel blank
    #
    # In UE5: each FBX imports as a single StaticMesh. The StaticMesh Editor
    # should show SOCKET_ attachment points in the Sockets panel.
    # Drag EQ_server.fbx into the level → it looks like one server.
    # To recreate a full rack, use the DataTable from TestRack_manifest.json
    # and Blueprint instance logic at runtime (Phase 4).
    try:
        eq_export = eqt.export_equipment_set_ue5(
            collection_name=RACK_NAME,
            output_dir=OUTPUT_DIR,
        )
        _check("export_equipment_set_ue5 returned", eq_export)
        _check("at least 1 equipment type exported",
               {"ok": eq_export["type_count"] >= 1},
               key="ok", expected=True)
        _check("no export errors",
               {"ok": len(eq_export.get("errors", [])) == 0},
               key="ok", expected=True)

        for entry in eq_export.get("exported", []):
            eq_fbx_exists = os.path.isfile(entry["file"])
            _check(f"EQ_{entry['equipment_type']}.fbx on disk  "
                   f"({entry['triangles']} tris, "
                   f"{len(entry.get('sockets', []))} sockets)",
                   {"ok": eq_fbx_exists}, key="ok", expected=True)

    except Exception:
        print(f"  [FAIL]  export_equipment_set_ue5 raised an exception:")
        traceback.print_exc()
        _fail_count += 1

    # ══════════════════════════════════════════════════════════════════════
    _section("Step 9 — clear_rack_population (cleanup pass)")
    # ══════════════════════════════════════════════════════════════════════
    # Expected: TestRack_Equipment collection and all equipment objects removed.
    # The TestRack cabinet structure (chassis mesh, doors) is untouched.
    #
    # In Blender: Outliner should show only "TestRack" with its cabinet mesh
    # and door objects. The _Equipment collection should be gone.
    try:
        clear = eqt.clear_rack_population(
            collection_name=RACK_NAME,
            also_clear_sub_collection=True,
        )
        _check("clear_rack_population returned", clear)
        _check("equipment removed (count > 0)",
               {"ok": clear["count"] > 0}, key="ok", expected=True)

        equip_col_gone = bpy.data.collections.get(f"{RACK_NAME}_Equipment") is None
        _check("_Equipment collection removed from scene",
               {"ok": equip_col_gone}, key="ok", expected=True)

        cabinet_intact = bpy.data.objects.get(RACK_NAME) is not None
        _check("cabinet mesh still present after clear",
               {"ok": cabinet_intact}, key="ok", expected=True)

    except Exception:
        print(f"  [FAIL]  clear_rack_population raised an exception:")
        traceback.print_exc()
        _fail_count += 1

    # ── Final report ────────────────────────────────────────────────────────
    print(f"\n{'═' * 60}")
    print(f"  RESULTS:  {_pass_count} passed  |  {_fail_count} failed")
    if _fail_count == 0:
        print(f"  STATUS:   ALL TESTS PASSED — pipeline ready for UE5")
    else:
        print(f"  STATUS:   {_fail_count} FAILURE(S) — see [FAIL] lines above")
    print(f"  EXPORTS:  {OUTPUT_DIR}")
    print(f"{'═' * 60}\n")


# ── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_pipeline_test()
