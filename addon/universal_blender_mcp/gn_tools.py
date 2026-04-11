"""
Geometry Nodes management tools for the Universal Blender MCP server (v2.0.0).

Provides tools to add, configure, query, and apply Geometry Nodes modifiers.
Designed for procedural rack panel generation, cable routing, and PCG workflows
that will be implemented in Phase 3 of the UPTIME asset pipeline.

All tools use @mcp.tool() + @thread_safe from core.py.
"""

import bpy
from typing import Any, Dict, List, Optional

from core import mcp, thread_safe, _log


# ── Tool 1: add_gn_modifier ───────────────────────────────────────────────

@mcp.tool()
@thread_safe
def add_gn_modifier(
    object_name: str,
    node_group_name: str = "",
    modifier_name: str = "GeometryNodes",
) -> Dict[str, Any]:
    """
    Add a Geometry Nodes modifier to an object.

    If node_group_name is given and a node group with that name exists in the
    blend file, it will be assigned to the modifier immediately.
    If node_group_name is empty, a new blank node group is created and assigned.

    object_name:      target object (any type — GN works on meshes, curves, etc.)
    node_group_name:  existing node group to assign (empty = create new)
    modifier_name:    name for the modifier (default 'GeometryNodes')
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")

    mod = obj.modifiers.new(name=modifier_name, type='NODES')

    if node_group_name:
        ng = bpy.data.node_groups.get(node_group_name)
        if not ng:
            raise ValueError(f"Node group '{node_group_name}' not found")
        mod.node_group = ng
        assigned_ng = node_group_name
    else:
        # Create a minimal pass-through node group
        ng = bpy.data.node_groups.new(name=f"GN_{object_name}", type='GeometryNodeTree')

        # Add Group Input and Group Output nodes with geometry socket
        in_node  = ng.nodes.new('NodeGroupInput')
        out_node = ng.nodes.new('NodeGroupOutput')
        in_node.location  = (-200, 0)
        out_node.location = ( 200, 0)

        # Blender 4.x API for adding interface sockets
        if hasattr(ng, 'interface'):
            ng.interface.new_socket('Geometry', in_out='INPUT',  socket_type='NodeSocketGeometry')
            ng.interface.new_socket('Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
        else:
            ng.inputs.new('NodeSocketGeometry',  'Geometry')
            ng.outputs.new('NodeSocketGeometry', 'Geometry')

        # Wire input → output (pass-through)
        if in_node.outputs and out_node.inputs:
            ng.links.new(in_node.outputs[0], out_node.inputs[0])

        mod.node_group = ng
        assigned_ng = ng.name

    return {
        "object":     object_name,
        "modifier":   mod.name,
        "node_group": assigned_ng,
    }


# ── Tool 2: set_gn_input ──────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def set_gn_input(
    object_name: str,
    modifier_name: str,
    input_name: str,
    value: Any,
) -> Dict[str, Any]:
    """
    Set a named input parameter on a Geometry Nodes modifier.

    Works with numeric (int, float), boolean, vector (list of 3 floats),
    and object reference inputs.

    object_name:   target object
    modifier_name: name of the GN modifier
    input_name:    name of the input socket (as shown in the modifier panel)
    value:         new value — use an object name (string) for object inputs
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")

    mod = obj.modifiers.get(modifier_name)
    if not mod or mod.type != 'NODES':
        raise ValueError(
            f"Geometry Nodes modifier '{modifier_name}' not found on '{object_name}'"
        )

    ng = mod.node_group
    if not ng:
        raise ValueError(f"Modifier '{modifier_name}' has no node group assigned")

    # Find the input identifier via the node group interface
    input_id = None
    if hasattr(ng, 'interface'):
        for item in ng.interface.items_tree:
            if hasattr(item, 'in_out') and item.in_out == 'INPUT' and item.name == input_name:
                input_id = item.identifier
                break
    else:
        for inp in ng.inputs:
            if inp.name == input_name:
                input_id = inp.identifier
                break

    if input_id is None:
        raise ValueError(f"Input '{input_name}' not found in node group '{ng.name}'")

    # Set the value
    if isinstance(value, str):
        ref_obj = bpy.data.objects.get(value)
        if not ref_obj:
            raise ValueError(f"Object reference '{value}' not found")
        mod[input_id] = ref_obj
    elif isinstance(value, list) and len(value) == 3:
        mod[input_id] = value
    else:
        mod[input_id] = value

    obj.update_tag()

    return {
        "object":    object_name,
        "modifier":  modifier_name,
        "input":     input_name,
        "value":     value,
    }


# ── Tool 3: get_gn_inputs ─────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def get_gn_inputs(
    object_name: str,
    modifier_name: str,
) -> Dict[str, Any]:
    """
    Return all exposed input parameters of a Geometry Nodes modifier.

    Shows each input's name, type, current value (if set), and identifier.
    Use this to discover what parameters a GN modifier exposes before
    calling set_gn_input.

    object_name:   target object
    modifier_name: name of the GN modifier
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")

    mod = obj.modifiers.get(modifier_name)
    if not mod or mod.type != 'NODES':
        raise ValueError(
            f"Geometry Nodes modifier '{modifier_name}' not found on '{object_name}'"
        )

    ng = mod.node_group
    if not ng:
        return {"object": object_name, "modifier": modifier_name, "inputs": [], "node_group": None}

    inputs = []
    if hasattr(ng, 'interface'):
        for item in ng.interface.items_tree:
            if hasattr(item, 'in_out') and item.in_out == 'INPUT':
                entry = {
                    "name":       item.name,
                    "type":       item.socket_type if hasattr(item, 'socket_type') else str(type(item).__name__),
                    "identifier": item.identifier,
                }
                # Try to read current value
                try:
                    entry["value"] = mod[item.identifier]
                except (KeyError, TypeError):
                    entry["value"] = None
                inputs.append(entry)
    else:
        for inp in ng.inputs:
            entry = {"name": inp.name, "type": inp.type, "identifier": inp.identifier}
            try:
                entry["value"] = mod[inp.identifier]
            except (KeyError, TypeError):
                entry["value"] = None
            inputs.append(entry)

    return {
        "object":     object_name,
        "modifier":   modifier_name,
        "node_group": ng.name,
        "inputs":     inputs,
    }


# ── Tool 4: list_gn_modifiers ─────────────────────────────────────────────

@mcp.tool()
@thread_safe
def list_gn_modifiers(object_name: str) -> Dict[str, Any]:
    """
    Return all Geometry Nodes modifiers on an object.

    Includes the modifier name, assigned node group name, and enabled state.

    object_name: target object
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")

    gn_mods = [
        {
            "name":       mod.name,
            "node_group": mod.node_group.name if mod.node_group else None,
            "show_viewport": mod.show_viewport,
            "show_render":   mod.show_render,
        }
        for mod in obj.modifiers
        if mod.type == 'NODES'
    ]

    return {
        "object":    object_name,
        "modifiers": gn_mods,
        "count":     len(gn_mods),
    }


# ── Tool 5: apply_gn_modifier ─────────────────────────────────────────────

@mcp.tool()
@thread_safe
def apply_gn_modifier(
    object_name: str,
    modifier_name: str,
) -> Dict[str, Any]:
    """
    Apply (collapse) a Geometry Nodes modifier, converting procedural geometry
    to static mesh data.

    WARNING: This is a destructive operation. The procedural modifier is
    removed and the mesh data is replaced with the evaluated result.
    Push an undo with execute_safe_python first if you may need to revert.

    object_name:   target mesh object
    modifier_name: name of the GN modifier to apply
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")

    mod = obj.modifiers.get(modifier_name)
    if not mod or mod.type != 'NODES':
        raise ValueError(
            f"Geometry Nodes modifier '{modifier_name}' not found on '{object_name}'"
        )

    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    bpy.ops.object.modifier_apply(modifier=modifier_name)

    return {
        "object":   object_name,
        "modifier": modifier_name,
        "result":   "Applied — modifier converted to static mesh data",
    }


# ── Tool 6: list_gn_node_groups ───────────────────────────────────────────

@mcp.tool()
@thread_safe
def list_gn_node_groups() -> List[Dict[str, Any]]:
    """
    Return all Geometry Nodes node groups in the current blend file.

    Includes the group name, number of exposed inputs, and how many objects
    reference it. Use this to find existing GN setups before calling
    add_gn_modifier with a node_group_name.
    """
    groups = []
    for ng in bpy.data.node_groups:
        if ng.type != 'GEOMETRY':
            continue

        input_count = 0
        if hasattr(ng, 'interface'):
            input_count = sum(
                1 for item in ng.interface.items_tree
                if hasattr(item, 'in_out') and item.in_out == 'INPUT'
            )
        else:
            input_count = len(ng.inputs)

        # Count objects that use this node group
        user_count = sum(
            1 for obj in bpy.data.objects
            for mod in obj.modifiers
            if mod.type == 'NODES' and mod.node_group == ng
        )

        groups.append({
            "name":        ng.name,
            "input_count": input_count,
            "users":       user_count,
            "fake_user":   ng.use_fake_user,
        })

    return sorted(groups, key=lambda g: g["name"])
