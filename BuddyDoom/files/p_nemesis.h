// Emacs style mode select   -*- C++ -*-
//-----------------------------------------------------------------------------
//
// DESCRIPTION:
//	Nemesis memory (JEV Director Tier 0) — public interface.
//	See p_nemesis.c for the design notes.
//
//-----------------------------------------------------------------------------

#ifndef __P_NEMESIS__
#define __P_NEMESIS__

#include "p_mobj.h"		// mobj_t

#define NEM_VERSION	1

// Resolve a monster type name (AI_TypeName vocabulary) to a row index, or -1.
int		NEM_TypeIndex (const char* type);

// Map a weaponinfo index (0..8) to a row column, or -1.
int		NEM_WeaponIndex (int weapon_idx);

// Name of a row (for logging).
const char*	NEM_TypeName (int idx);

// Row vocabulary name for a mobj type, or NULL if the type isn't tracked.
const char*	NEM_TypeNameOf (mobjtype_t t);

// Ground-truth hooks. Call from P_DamageMobj / P_KillMobj paths.
// `type` is the AI_TypeName() vocabulary; `weapon_idx` is the source player's
// readyweapon (or -1 when the attacker isn't a player).
void		NEM_NoteDamageM (const char* type, mobj_t* source, int damage, int weapon_idx);
void		NEM_NoteKill (const char* type, mobj_t* source, int weapon_idx);

// mobj-based convenience wrappers used from p_inter.c (map mobjtype -> row).
void		NEM_NoteHit (mobj_t* target, mobj_t* source, int damage, int weapon_idx);
void		NEM_NoteKillM (mobj_t* target, mobj_t* source, int weapon_idx);

// Tactic-in-effect bookkeeping (called from p_ai_llm.c when directives apply).
void		NEM_NoteTactic (const char* type, const char* order);

// Push a label into the observed event ring (used internally, exposed for tests).
void		NEM_PushEvent (const char* label);

// Serialize the table + event ring into an observe payload fragment:
//   "nemesis":{...}   (caller wraps it into the JSON object)
int		NEM_Serialize (char* buf, int buflen);

// Native consumption (no model needed): the rule director weights its tactic
// selection with these.
float		NEM_TacticWeight (const char* type, const char* order);
float		NEM_WeaponBias (const char* type, int weapon_idx);
int		NEM_Deaths (const char* type);
const char*	NEM_LastTactic (const char* type);

// Decay pass: call once per second of level time from the director ticker.
void		NEM_DecayPass (void);

// Apply one clamped proposal. Returns "ok" or a short error string.
const char*	NEM_Propose (const char* type, const char* key, const char* value);

// Phase 9: hostile-buddy skill curriculum (0=rookie .. 4=legend).  Lives in
// the nemesis store: persisted in nemesis_memory.dat, serialized in observe.
int		NEM_BuddySkill (void);
void		NEM_SetBuddySkill (int level);
// Auto-lesson: the hostile buddy killed the human -> ease off one step.
void		NEM_NoteBuddyKillPlayer (void);
// Phase 9.11: the hostile buddy absorbed `damage` from the human -> XP toward
// its next rank (auto-promotion every NEM_BUDDY_XP points).  Also driven by
// the director protocol (`buddy xp=N`) so tools can exercise the ladder.
void		NEM_NoteBuddyDamage (int damage);
// XP progress toward the next rank (0..99), serialized as buddy_xp.
int		NEM_BuddyXP (void);

// Persistence (nemesis_memory.dat sibling of buddydoom.cfg).
void		NEM_Load (void);
void		NEM_Save (void);
const char*	NEM_DirPrefix (void);

// Lazy init (loads the table). Safe to call repeatedly.
void		NEM_Init (void);

// Save + reset (call from I_Quit).
void		NEM_Quit (void);

// Live metrics HUD (display-only; set by the external RL agent via
// "nemesis ... hud=ep=N,eps=F,r=F,surv=F,act=NAME" lines).
void		NEM_HUDSet (const char* hud_spec);
void		NEM_HUDPrint (void);

#endif
