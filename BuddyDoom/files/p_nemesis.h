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

// Persistence (nemesis_memory.dat sibling of buddydoom.cfg).
void		NEM_Load (void);
void		NEM_Save (void);
const char*	NEM_DirPrefix (void);

// Lazy init (loads the table). Safe to call repeatedly.
void		NEM_Init (void);

// Save + reset (call from I_Quit).
void		NEM_Quit (void);

#endif
