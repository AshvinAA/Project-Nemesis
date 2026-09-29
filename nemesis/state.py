"""The agreed 576-cell discretized state (3 x 2 x 4 x 4 x 3 x 2).

Every component is computed ONLY from fields the director observe actually
carries (verified against AI_Serialize):
    monsters[]: id, type, pos[x,y,z], hp, region, see_player, d_player, order
    player:     pos[x,y,z], angle(deg), health, armor, weapon, region
    regions[]:  [id, x, y] centroids
    links[]:    [a, b, "open"|"door"|"locked"]

Documented approximations (see D-ledger in docs/NEMESIS_RL_PLAN.md):
- ANGLE bucket is measured from the PLAYER's view (bear_monster relative to
  player facing), because monster facing is not serialized. front = monster
  inside the player's aim (about to be shot), behind = monster has the drop
  on the player. Tactically that is the signal that matters.
- COVER is approximated from the region link-graph: non-open links ("door"/
  "locked") adjacent to the monster's region, sided by cross product against
  the monster->player axis. Coarse, but deterministic and cheap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# --- tunables (map-constant once training is pinned; see D3/D6) -------------
DIST_CLOSE = 250.0        # map units: inside shotgun-flinch range
DIST_FAR = 700.0          # beyond this the fight is a manhunt, not a duel
HP_HEALTHY = 2.0 / 3.0    # fraction of spawnhealth (30 -> >20 hp)
HP_HURT = 1.0 / 3.0       # 30 -> 11..20
ANGLE_FRONT = 45.0        # deg, player's aim cone
ANGLE_BEHIND = 135.0      # deg beyond this = behind the player

DIMS = (3, 2, 4, 4, 3, 2)                 # dist, los, angle, cover, hp, hit
STATE_COUNT = DIMS[0]*DIMS[1]*DIMS[2]*DIMS[3]*DIMS[4]*DIMS[5]  # 576

DIST_NAMES = ("close", "medium", "far")
LOS_NAMES = ("blocked", "visible")
ANGLE_NAMES = ("front", "flank_left", "flank_right", "behind")
COVER_NAMES = ("none", "left", "right", "both")
HP_NAMES = ("critical", "hurt", "healthy")
HIT_NAMES = ("no", "yes")


@dataclass(frozen=True)
class StateVector:
    dist: int      # 0 close / 1 medium / 2 far
    los: int       # 0 blocked / 1 visible
    angle: int     # 0 front / 1 flank_left / 2 flank_right / 3 behind
    cover: int     # 0 none / 1 left / 2 right / 3 both
    hp: int        # 0 critical / 1 hurt / 2 healthy
    hit_recent: int  # 0 no / 1 yes

    def index(self) -> int:
        return ((((self.dist * 2 + self.los) * 4 + self.angle) * 4 + self.cover
                 ) * 3 + self.hp) * 2 + self.hit_recent

    def name(self) -> str:
        return (f"{DIST_NAMES[self.dist]}/{LOS_NAMES[self.los]}/"
                f"{ANGLE_NAMES[self.angle]}/{COVER_NAMES[self.cover]}/"
                f"{HP_NAMES[self.hp]}/hit={HIT_NAMES[self.hit_recent]}")


def _dist_bucket(d: Optional[float]) -> int:
    if d is None:
        return 2  # unknown == far (conservative: don't reward closing blind)
    if d <= DIST_CLOSE:
        return 0
    if d <= DIST_FAR:
        return 1
    return 2


def _angle_bucket(nx: float, ny: float, px: float, py: float, player_angle_deg: float) -> int:
    """Monster position relative to the player's facing (see module docstring).
    Doom map coords: x east, y north, angle 0 = +x, CCW positive. Bearing is
    PLAYER->MONSTER; rel > 0 (CCW of facing) = the player's LEFT."""
    bearing = math.degrees(math.atan2(ny - py, nx - px))
    rel = (bearing - player_angle_deg + 180.0) % 360.0 - 180.0
    a = abs(rel)
    if a <= ANGLE_FRONT:
        return 0
    if a >= ANGLE_BEHIND:
        return 3
    return 1 if rel > 0 else 2


def _cover_bucket(region_id: Optional[int], monster_xy: tuple[float, float],
                  player_xy: tuple[float, float],
                  regions: dict, links: list) -> int:
    """Non-open links adjacent to the monster's region, sided vs the axis to
    the player. left/right/both/none."""
    if region_id is None or region_id not in regions:
        return 0
    mx, my = monster_xy
    px, py = player_xy
    axis = (px - mx, py - my)
    ax_len = math.hypot(*axis) or 1.0
    left = right = False
    for a, b, kind in links:
        if region_id not in (a, b) or kind == "open":
            continue
        other = b if a == region_id else a
        if other not in regions:
            continue
        ox, oy = regions[other]
        rel = (ox - mx, oy - my)
        # cross product z-component: >0 == neighbor on the LEFT of the axis
        cross = axis[0] * rel[1] - axis[1] * rel[0]
        if cross > 0:
            left = True
        else:
            right = True
    if left and right:
        return 3
    if left:
        return 1
    if right:
        return 2
    return 0


def _hp_bucket(hp: Optional[int], spawnhealth: int) -> int:
    if hp is None:
        return 0
    frac = max(0, hp) / float(spawnhealth)
    if frac > HP_HEALTHY:
        return 2
    if frac > HP_HURT:
        return 1
    return 0


def compile_state(nem: dict, player: dict, prev_hp: Optional[int],
                  regions: dict, links: list) -> StateVector:
    """nem: the monsters[] row for our Shotgun Guy. player: the player dict.
    prev_hp: hp from the previous poll (None on first sight). regions: {id:(x,y)}.
    links: [(a,b,kind)]."""
    d = nem.get("d_player")
    los = 1 if nem.get("see_player") else 0
    mx, my = nem.get("pos", [0, 0, 0])[0:2]
    px, py = player.get("pos", [0, 0, 0])[0:2]
    angle = _angle_bucket(mx, my, px, py, float(player.get("angle", 0)))
    cover = _cover_bucket(nem.get("region"), (mx, my), (px, py), regions, links)
    hp = _hp_bucket(nem.get("hp"), 30)
    hit = 1 if (prev_hp is not None and nem.get("hp") is not None
                and nem["hp"] < prev_hp) else 0
    return StateVector(_dist_bucket(d), los, angle, cover, hp, hit)


def regions_links_from_obs(obs: dict) -> tuple[dict, list]:
    regions = {r[0]: (r[1], r[2]) for r in obs.get("regions", []) if len(r) >= 3}
    links = [(l[0], l[1], l[2]) for l in obs.get("links", []) if len(l) >= 3]
    return regions, links
