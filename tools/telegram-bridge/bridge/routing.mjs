// routing.mjs — chat_id → session label (ADR-0106). The routing table is the
// layer the stock Telegram plugin lacks: it maps each operator chat to exactly
// one session, preserving the ADR-0067 "chat list = session list" phone UX.
//
// The table is config data, loaded from JSON. A message whose chat_id has no
// mapping is NOT delivered anywhere (it is logged and dropped) — silent
// mis-delivery to a wrong session is the ADR-0106 falsifier, so an unknown
// chat fails closed, never fanned out.

import { readFileSync } from 'node:fs'
import { assertLabel } from './inject.mjs'

// Parse + validate a routing config object. Shape:
//   { "routes": { "<chat_id>": "<session-label>", ... } }
// chat_id keys are stringified integers; labels are validated as path-safe.
export function parseRouting(obj) {
  if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) {
    throw new Error('routing config must be an object')
  }
  const routes = obj.routes
  if (routes === null || typeof routes !== 'object' || Array.isArray(routes)) {
    throw new Error('routing config must have a "routes" object')
  }
  const table = new Map()
  for (const [chatId, label] of Object.entries(routes)) {
    if (!/^-?\d+$/.test(chatId)) {
      throw new Error(`route chat_id must be an integer string: ${JSON.stringify(chatId)}`)
    }
    assertLabel(label)
    table.set(chatId, label)
  }
  return table
}

export function loadRouting(path) {
  return parseRouting(JSON.parse(readFileSync(path, 'utf8')))
}

// Resolve a chat_id (number or string) to a label, or null if unmapped.
export function resolveLabel(table, chatId) {
  return table.get(String(chatId)) ?? null
}
