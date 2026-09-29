"""Action -> engine-director mapping.

The 8 abstract Q-actions must land on the engine's EXACT order vocabulary
(AI_OrderByName): chase hold fallback flank_left flank_right ambush
focus_fire use_door. Movement intent only — attack gates stay vanilla
(the no-stat-buffs rule; A_LLMChase steers movement, never weapon behavior).

Mapping (given the freshly compiled state):
    advance          -> chase                       (close with the player)
    retreat          -> fallback                    (open the distance)
    strafe_left      -> flank_left                  (side-step left)
    strafe_right     -> flank_right                 (side-step right)
    take_cover       -> hold, anchored behind cover (see below)
    stand_and_shoot  -> focus_fire                  (hold ground, keep firing)
    flank_left/right -> flank_left/right with an anchor point on the player's
                        far side (the engine paths around obstacles)

take_cover detail: if the state reports cover on one side, we anchor the
monster at its CURRENT region's cover-sibling region (the non-open link we
detected); otherwise it's a plain hold (duck in place — vanilla semantics).
"""

from __future__ import annotations

from typing import Optional

from .qtable import ACTIONS


def action_to_order(action: int, nem: Optional[dict], player: Optional[dict],
                    regions: dict, links: list) -> tuple[str, Optional[tuple[int, int]]]:
    """Returns (order_name, anchor_xy). anchor_xy is an x=/y= hint when useful;
    the engine treats a zero x/y as 'no waypoint' (AI_Apply default)."""
    name = ACTIONS[action]
    if name == "advance":
        return "chase", None
    if name == "retreat":
        return "fallback", None
    if name == "strafe_left":
        return "flank_left", None
    if name == "strafe_right":
        return "flank_right", None
    if name == "stand_and_shoot":
        return "focus_fire", None
    if name == "take_cover":
        anchor = _cover_anchor(nem, player, regions, links)
        if anchor is not None:
            return "hold", anchor
        return "hold", None
    if name == "flank_left":
        return "flank_left", _flank_anchor(nem, player, "left")
    if name == "flank_right":
        return "flank_right", _flank_anchor(nem, player, "right")
    return "chase", None  # unreachable


def _cover_anchor(nem: Optional[dict], player: Optional[dict],
                  regions: dict, links: list) -> Optional[tuple[int, int]]:
    """Anchor at the cover-sibling region (a non-open link neighbor), if any."""
    if not nem or not player:
        return None
    rid = nem.get("region")
    if rid is None or rid not in regions:
        return None
    for a, b, kind in links:
        if rid in (a, b) and kind != "open":
            other = b if a == rid else a
            if other in regions:
                x, y = regions[other]
                return (int(x), int(y))
    return None


def _flank_anchor(nem: Optional[dict], player: Optional[dict],
                  side: str) -> Optional[tuple[int, int]]:
    """Anchor on the player's flank: their position offset perpendicular to
    the nemesis->player axis. The engine's flank orders do their own routing;
    this just biases the waypoint to the correct side."""
    if not nem or not player:
        return None
    try:
        mx, my = nem["pos"][0], nem["pos"][1]
        px, py = player["pos"][0], player["pos"][1]
    except (KeyError, IndexError, TypeError):
        return None
    import math
    ax, ay = px - mx, py - my
    length = math.hypot(ax, ay) or 1.0
    # perpendicular (left of the axis in doom's y-up CCW frame)
    sign = 1 if side == "left" else -1
    nx, ny = -ay / length * sign, ax / length * sign
    return (int(px + nx * 128), int(py + ny * 128))
