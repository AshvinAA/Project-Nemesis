// Emacs style mode select   -*- C++ -*-
//-----------------------------------------------------------------------------
//
// DESCRIPTION:
//	Nemesis memory (JEV Director Tier 0).  A deliberately dumb, persistent
//	per-monster-type table: tactic weights, weapon bias, death counters, and
//	a small ground-truth event ring fed by P_DamageMobj / P_KillMobj.
//
//	The table lives in the ENGINE so that:
//	  - learned behavior survives bridge/API failure (the rule director
//	    consumes it directly, no model call needed);
//	  - every hit and kill is ground truth, attributed at the moment it
//	    happens (P_DamageMobj knows the weapon);
//	  - external learning (the JEV bridge) can only PROPOSE deltas, and the
//	    engine clamps everything: a buggy or hostile bridge cannot unbalance
//	    the table beyond the clamps below.
//
//	All functions are non-blocking and tic-path safe: no allocation, no
//	pointer storage across the protocol, file I/O only at level start/end.
//
//-----------------------------------------------------------------------------

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "doomtype.h"
#include "doomdef.h"		// MF_COUNTKILL
#include "doomstat.h"		// players, consoleplayer, gametic, gameskill
#include "info.h"		// mobjinfo_t
#include "d_items.h"		// weaponinfo
#include "p_nemesis.h"
#include "p_mobj.h"

// ---------------------------------------------------------------------------
// Tunables (the clamps that bound what any external proposal can do)
// ---------------------------------------------------------------------------

#define NEM_WFLOOR	0.05f		// tactic weights can't collapse to 0
#define NEM_WCEIL	2.0f		// ...or explode
#define NEM_BFLOOR	-1.0f		// weapon bias range
#define NEM_BCEIL	 1.0f
#define NEM_MAXPROPOSAL	0.5f		// max |delta| per proposal line
#define NEM_DECAY	0.995f		// per-second multiplicative decay toward 1
#define NEM_EVENTMAX	16		// event ring size (labels observed by bridge)

#define NEM_TYPES	14		// rows below
#define NEM_WEAPONS	9
#define NEM_ORDERS	8

// Row order must match p_nemesis.h NEM_* indices.
static const char* nem_type_names[NEM_TYPES] =
{
    "zombie", "shotgun", "chaingun", "imp", "pinky", "spectre", "lost",
    "caco", "pain", "knight", "baron", "revenant", "mancubus", "arachnotron"
};

// Weapon order matches d_items weaponinfo indices (0..8).
static const char* nem_weapon_names[NEM_WEAPONS] =
{
    "fist", "pistol", "shotgun", "chaingun", "rocket", "plasma", "bfg",
    "chainsaw", "ssg"
};

// Order order matches p_ai_llm.c's AIO enum (parser names, exact).
static const char* nem_order_names[NEM_ORDERS] =
{
    "chase", "hold", "fallback", "flank_left", "flank_right",
    "focus_fire", "ambush", "use_door"
};

typedef struct
{
    float	tactics[NEM_ORDERS];		// weight per order (1 = neutral)
    float	weapon_bias[NEM_WEAPONS];	// -1..1 per player weapon
    int		deaths;
    int		deaths_by_weapon[NEM_WEAPONS];
} nemrow_t;

static nemrow_t	nem_rows[NEM_TYPES];
static int	nem_initialized;

// Ground-truth event ring. Labels are unique (index embedded) so the bridge
// can dedupe with a cursor. Kinds: "hit" and "kill".
static char	nem_events[NEM_EVENTMAX][48];
static long	nem_event_seq;
static int	nem_event_head;
static int	nem_event_count;

// Recent-tactic note: what directive each monster type is currently executing
// (set by p_ai_llm.c when a directive is applied, read at death time).
static char	nem_last_tactic[NEM_TYPES][16];

// ---------------------------------------------------------------------------
// Index resolution
// ---------------------------------------------------------------------------

int NEM_TypeIndex (const char* type)
{
    int i;
    if (!type) return -1;
    for (i = 0; i < NEM_TYPES; i++)
	if (!strcmp (type, nem_type_names[i])) return i;
    return -1;
}

int NEM_WeaponIndex (int weapon_idx)
{
    if (weapon_idx < 0 || weapon_idx >= NEM_WEAPONS) return -1;
    return weapon_idx;			// weaponinfo order == our order
}

static int NEM_OrderIndex (const char* order)
{
    int i;
    if (!order) return -1;
    for (i = 0; i < NEM_ORDERS; i++)
	if (!strcmp (order, nem_order_names[i])) return i;
    return -1;
}

const char* NEM_TypeName (int idx)
{
    return (idx >= 0 && idx < NEM_TYPES) ? nem_type_names[idx] : "monster";
}

// ---------------------------------------------------------------------------
// Hooks from the damage path (ground truth). Called from P_DamageMobj.
// The caller resolves the type name (it already owns the type switch), and
// `weapon_idx` is the source player's readyweapon, or -1 if not a player hit.
// ---------------------------------------------------------------------------

void NEM_NoteDamageM (const char* type, mobj_t* source, int damage, int weapon_idx)
{
    int ti, wi;
    char label[48];
    if (!type || !source || !source->player || damage <= 0) return;
    ti = NEM_TypeIndex (type);
    if (ti < 0) return;
    wi = NEM_WeaponIndex (weapon_idx);

    // Ground-truth event for the bridge (and our own continuous counters).
    if (wi >= 0)
    {
	nem_rows[ti].weapon_bias[wi] += 0.002f * (damage > 40 ? 2 : 1);
	if (nem_rows[ti].weapon_bias[wi] > NEM_BCEIL) nem_rows[ti].weapon_bias[wi] = NEM_BCEIL;
	snprintf (label, sizeof(label), "%ld:hit:%s:%s:%d",
		  nem_event_seq++, nem_type_names[ti], nem_weapon_names[wi], damage);
	NEM_PushEvent (label);
    }
}

void NEM_NoteKill (const char* type, mobj_t* source, int weapon_idx)
{
    int ti, wi;
    char label[48];
    if (!type) return;
    ti = NEM_TypeIndex (type);
    if (ti < 0) return;
    nem_rows[ti].deaths++;
    wi = NEM_WeaponIndex (weapon_idx);
    if (wi >= 0)
    {
	nem_rows[ti].deaths_by_weapon[wi]++;
	snprintf (label, sizeof(label), "%ld:kill:%s:%s",
		  nem_event_seq++, nem_type_names[ti], nem_weapon_names[wi]);
	NEM_PushEvent (label);
    }
    (void)source;
}

// Called by p_ai_llm.c whenever a directive is applied to a monster of `type`:
// remember the tactic in effect for death-time attribution.
void NEM_NoteTactic (const char* type, const char* order)
{
    int ti = NEM_TypeIndex (type);
    if (ti < 0 || !order) return;
    strncpy (nem_last_tactic[ti], order, sizeof(nem_last_tactic[0]) - 1);
    nem_last_tactic[ti][sizeof(nem_last_tactic[0]) - 1] = 0;
}

void NEM_PushEvent (const char* label)
{
    if (!label) return;
    strncpy (nem_events[nem_event_head], label, sizeof(nem_events[0]) - 1);
    nem_events[nem_event_head][sizeof(nem_events[0]) - 1] = 0;
    nem_event_head = (nem_event_head + 1) % NEM_EVENTMAX;
    if (nem_event_count < NEM_EVENTMAX) nem_event_count++;
}

// ---------------------------------------------------------------------------
// mobj-based entry points (used from p_inter.c): map the mobj type to our
// row vocabulary, then record. `weapon_idx` = source player's readyweapon.
// ---------------------------------------------------------------------------

static const char* NEM_MTypeName (mobjtype_t t)
{
    switch (t)
    {
      case MT_POSSESSED:	return "zombie";
      case MT_SHOTGUY:		return "shotgun";
      case MT_CHAINGUY:		return "chaingun";
      case MT_TROOP:		return "imp";
      case MT_SERGEANT:		return "pinky";
      case MT_SHADOWS:		return "spectre";
      case MT_SKULL:		return "lost";
      case MT_HEAD:		return "caco";
      case MT_PAIN:		return "pain";
      case MT_KNIGHT:		return "knight";
      case MT_BRUISER:		return "baron";
      case MT_UNDEAD:		return "revenant";
      case MT_FATSO:		return "mancubus";
      case MT_BABY:		return "arachnotron";
      default:			return NULL;	// not a tracked monster type
    }
}

// Public wrapper for other modules (spawn mixing in p_ai_director.c).
// Returns the row vocabulary name for a mobj type, or NULL if untracked.
const char* NEM_TypeNameOf (mobjtype_t t)
{
    return NEM_MTypeName (t);
}

// From P_DamageMobj, after `target->health -= damage`, when it survived.
void NEM_NoteHit (mobj_t* target, mobj_t* source, int damage, int weapon_idx)
{
    const char* tn;
    if (!target || target->health <= 0) return;		// deaths are recorded separately
    if (!source || !source->player) return;		// only player-inflicted hits teach
    tn = NEM_MTypeName (target->type);
    if (!tn) return;
    NEM_NoteDamageM (tn, source, damage, weapon_idx);
}

// From the kill path in P_DamageMobj (target died).
void NEM_NoteKillM (mobj_t* target, mobj_t* source, int weapon_idx)
{
    const char* tn;
    if (!target) return;
    tn = NEM_MTypeName (target->type);
    if (!tn) return;
    NEM_NoteKill (tn, source, weapon_idx);
}

// ---------------------------------------------------------------------------
// Serialization into the observe stream (called from AI_Serialize)
// ---------------------------------------------------------------------------

int NEM_Serialize (char* buf, int buflen)
{
    int n = 0, i, w, o;
    int firstrow;
    if (!buf || buflen <= 0) return 0;
    if (!nem_initialized) NEM_Init ();

    n += snprintf (buf + n, buflen - n, "\"nemesis\":{\"version\":%d,\"rows\":[", NEM_VERSION);
    firstrow = 1;
    for (i = 0; i < NEM_TYPES; i++)
    {
	nemrow_t* r = &nem_rows[i];
	// Only rows with any learned signal are emitted (token economy).
	int active = r->deaths > 0;
	for (o = 0; o < NEM_ORDERS && !active; o++)
	    if (r->tactics[o] < 0.95f || r->tactics[o] > 1.05f) active = 1;
	for (w = 0; w < NEM_WEAPONS && !active; w++)
	    if (r->weapon_bias[w] < -0.05f || r->weapon_bias[w] > 0.05f) active = 1;
	if (!active) continue;

	n += snprintf (buf + n, buflen - n, "%s{\"type\":\"%s\",\"deaths\":%d,\"tactics\":{",
		       firstrow ? "" : ",", nem_type_names[i], r->deaths);
	firstrow = 0;
	{
	    int first = 1;
	    for (o = 0; o < NEM_ORDERS; o++)
	    {
		if (r->tactics[o] < 0.95f || r->tactics[o] > 1.05f)
		{
		    n += snprintf (buf + n, buflen - n, "%s\"%s\":%.2f",
				   first ? "" : ",", nem_order_names[o], r->tactics[o]);
		    first = 0;
		}
	    }
	}
	n += snprintf (buf + n, buflen - n, "},\"weapon_bias\":{");
	{
	    int first = 1;
	    for (w = 0; w < NEM_WEAPONS; w++)
	    {
		if (r->weapon_bias[w] < -0.05f || r->weapon_bias[w] > 0.05f)
		{
		    n += snprintf (buf + n, buflen - n, "%s\"%s\":%.2f",
				   first ? "" : ",", nem_weapon_names[w], r->weapon_bias[w]);
		    first = 0;
		}
	    }
	}
	n += snprintf (buf + n, buflen - n, "}}");
	if (n > buflen - 256) break;
    }
    n += snprintf (buf + n, buflen - n, "],\"events\":[");

    {
	int e, first = 1;
	for (e = 0; e < nem_event_count; e++)
	{
	    int idx = (nem_event_head - 1 - e + NEM_EVENTMAX * 2) % NEM_EVENTMAX;
	    n += snprintf (buf + n, buflen - n, "%s\"%s\"", first ? "" : ",", nem_events[idx]);
	    first = 0;
	    if (n > buflen - 128) break;
	}
    }
    n += snprintf (buf + n, buflen - n, "]}");
    return n;
}

// ---------------------------------------------------------------------------
// Consumption by the rule director / tactic selector (native, no model needed)
// ---------------------------------------------------------------------------

float NEM_TacticWeight (const char* type, const char* order)
{
    int ti = NEM_TypeIndex (type);
    int oi = NEM_OrderIndex (order);
    if (!nem_initialized) NEM_Init ();
    if (ti < 0 || oi < 0) return 1.0f;
    return nem_rows[ti].tactics[oi];
}

float NEM_WeaponBias (const char* type, int weapon_idx)
{
    int ti = NEM_TypeIndex (type);
    int wi = NEM_WeaponIndex (weapon_idx);
    if (!nem_initialized) NEM_Init ();
    if (ti < 0 || wi < 0) return 0.0f;
    return nem_rows[ti].weapon_bias[wi];
}

int NEM_Deaths (const char* type)
{
    int ti = NEM_TypeIndex (type);
    if (!nem_initialized) NEM_Init ();
    return (ti >= 0) ? nem_rows[ti].deaths : 0;
}

const char* NEM_LastTactic (const char* type)
{
    int ti = NEM_TypeIndex (type);
    if (ti < 0) return "chase";
    return nem_last_tactic[ti][0] ? nem_last_tactic[ti] : "chase";
}

// ---------------------------------------------------------------------------
// Decay pass: call this from the director ticker every second of level time.
// Weights relax toward 1.0, bias toward 0 — the AI can't stay maxed forever,
// but the timescale (0.5%/s) means a session's learning persists for minutes.
// ---------------------------------------------------------------------------

void NEM_DecayPass (void)
{
    int i, o, w;
    for (i = 0; i < NEM_TYPES; i++)
    {
	nemrow_t* r = &nem_rows[i];
	for (o = 0; o < NEM_ORDERS; o++)
	    r->tactics[o] += (1.0f - r->tactics[o]) * (1.0f - NEM_DECAY);
	for (w = 0; w < NEM_WEAPONS; w++)
	    r->weapon_bias[w] += (0.0f - r->weapon_bias[w]) * (1.0f - NEM_DECAY);
    }
}

// ---------------------------------------------------------------------------
// Proposals from the bridge: "nemesis propose=<type> tactic_weight=<order>:<delta>"
// (parsed in p_ai_llm.c, applied here). Everything is clamped.
// ---------------------------------------------------------------------------

const char* NEM_Propose (const char* type, const char* key, const char* value)
{
    int ti = NEM_TypeIndex (type);
    if (ti < 0) return "bad-type";
    if (!nem_initialized) NEM_Init ();

    if (!strncmp (key, "tactic_weight=", 14))
    {
	char order[24];
	float delta;
	char* colon = strchr (key + 14, ':');
	size_t len;
	int oi;
	if (!colon) return "bad-key";
	len = (size_t)(colon - (key + 14));
	if (len == 0 || len >= sizeof(order)) return "bad-order";
	memcpy (order, key + 14, len);
	order[len] = 0;
	oi = NEM_OrderIndex (order);
	if (oi < 0) return "bad-order";
	delta = (float)atof (colon + 1);
	if (delta > NEM_MAXPROPOSAL) delta = NEM_MAXPROPOSAL;
	if (delta < -NEM_MAXPROPOSAL) delta = -NEM_MAXPROPOSAL;
	nem_rows[ti].tactics[oi] += delta;
	if (nem_rows[ti].tactics[oi] < NEM_WFLOOR) nem_rows[ti].tactics[oi] = NEM_WFLOOR;
	if (nem_rows[ti].tactics[oi] > NEM_WCEIL) nem_rows[ti].tactics[oi] = NEM_WCEIL;
	return "ok";
    }
    if (!strncmp (key, "bias=", 5))
    {
	// bias=<countermeasure>: bias is expressed as tactic-weight nudges.
	float d = 0.15f;
	if      (!strcmp (value, "range"))  { NEM_Propose (type, "tactic_weight=hold:-0.1", ""); NEM_Propose (type, "tactic_weight=fallback:0.15", ""); return "ok"; }
	else if (!strcmp (value, "aggression")) { NEM_Propose (type, "tactic_weight=focus_fire:0.2", ""); NEM_Propose (type, "tactic_weight=chase:0.1", ""); return "ok"; }
	else if (!strcmp (value, "unpredictability")) { NEM_Propose (type, "tactic_weight=flank_left:0.2", ""); NEM_Propose (type, "tactic_weight=ambush:0.2", ""); return "ok"; }
	else if (!strcmp (value, "cover")) { NEM_Propose (type, "tactic_weight=use_door:0.15", ""); NEM_Propose (type, "tactic_weight=hold:0.1", ""); return "ok"; }
	(void)d;
	return "ok";
    }
    return "bad-key";
}

// ---------------------------------------------------------------------------
// Persistence: nemesis_memory.dat, sibling of buddydoom.cfg.
// Simple binary layout, version-tagged.
// ---------------------------------------------------------------------------

void NEM_Load (void)
{
    FILE* f;
    char path[512];
    int ver = 0, i;
    if (!nem_initialized) NEM_Init ();
    snprintf (path, sizeof(path), "%snemesis_memory.dat", NEM_DirPrefix ());
    f = fopen (path, "rb");
    if (!f) return;					// fresh install: neutral table
    if (fread (&ver, sizeof(ver), 1, f) == 1 && ver == NEM_VERSION)
    {
	for (i = 0; i < NEM_TYPES; i++)
	{
	    nemrow_t* r = &nem_rows[i];
	    if (fread (r->tactics, sizeof(float), NEM_ORDERS, f) != NEM_ORDERS) break;
	    if (fread (r->weapon_bias, sizeof(float), NEM_WEAPONS, f) != NEM_WEAPONS) break;
	    if (fread (&r->deaths, sizeof(int), 1, f) != 1) break;
	    if (fread (r->deaths_by_weapon, sizeof(int), NEM_WEAPONS, f) != NEM_WEAPONS) break;
	}
    }
    fclose (f);
}

void NEM_Save (void)
{
    FILE* f;
    char path[512];
    int ver = NEM_VERSION, i;
    if (!nem_initialized) NEM_Init ();
    snprintf (path, sizeof(path), "%snemesis_memory.dat", NEM_DirPrefix ());
    f = fopen (path, "wb");
    if (!f) return;
    fwrite (&ver, sizeof(ver), 1, f);
    for (i = 0; i < NEM_TYPES; i++)
    {
	nemrow_t* r = &nem_rows[i];
	fwrite (r->tactics, sizeof(float), NEM_ORDERS, f);
	fwrite (r->weapon_bias, sizeof(float), NEM_WEAPONS, f);
	fwrite (&r->deaths, sizeof(int), 1, f);
	fwrite (r->deaths_by_weapon, sizeof(int), NEM_WEAPONS, f);
    }
    fclose (f);
}

// Working-directory prefix: keep it simple and portable — the game runs from
// run/ where buddydoom.cfg lives, so a bare relative name matches that
// convention. Overridable for tools/tests via the NEM_DIR env var.
const char* NEM_DirPrefix (void)
{
    static char prefix[400];
    static int resolved;
    if (!resolved)
    {
	const char* env = getenv ("NEM_DIR");
	snprintf (prefix, sizeof(prefix), "%s", (env && *env) ? env : "");
	resolved = 1;
    }
    return prefix;
}

void NEM_Quit (void)
{
    if (!nem_initialized) return;
    NEM_Save ();
    nem_initialized = 0;
}

void NEM_Init (void)
{
    int i, o, w;
    if (nem_initialized) return;
    for (i = 0; i < NEM_TYPES; i++)
    {
	nemrow_t* r = &nem_rows[i];
	for (o = 0; o < NEM_ORDERS; o++) r->tactics[o] = 1.0f;
	for (w = 0; w < NEM_WEAPONS; w++) r->weapon_bias[w] = 0.0f;
	r->deaths = 0;
	for (w = 0; w < NEM_WEAPONS; w++) r->deaths_by_weapon[w] = 0;
	memset (nem_last_tactic[i], 0, sizeof(nem_last_tactic[0]));
    }
    nem_event_seq = 0;
    nem_event_head = 0;
    nem_event_count = 0;
    nem_initialized = 1;
    NEM_Load ();
}
