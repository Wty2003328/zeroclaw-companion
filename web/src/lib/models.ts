/**
 * Live2D model selection: lists installed models from the server,
 * persists the user's pick to localStorage so it overrides the
 * server-default model that arrives via the WS ModelInfo frame.
 */

import { HTTP_BASE } from './apiBase';

export interface InstalledModel {
  id: string;
  name: string;
  modelUrl: string;
  format: 'cubism2' | 'cubism4' | string;
}

export async function fetchInstalledModels(): Promise<InstalledModel[]> {
  try {
    const r = await fetch(`${HTTP_BASE}/api/models`);
    if (!r.ok) return [];
    const j = await r.json();
    if (!Array.isArray(j?.models)) return [];
    return j.models;
  } catch {
    return [];
  }
}

const MODEL_KEY = 'companion.userModel.v1';

/** The user's chosen model id, or null to defer to the server default. */
export function getUserModelChoice(): string | null {
  try {
    return localStorage.getItem(MODEL_KEY);
  } catch {
    return null;
  }
}

export function setUserModelChoice(id: string | null): void {
  try {
    if (id === null || id === '') {
      localStorage.removeItem(MODEL_KEY);
    } else {
      localStorage.setItem(MODEL_KEY, id);
    }
  } catch { /* non-fatal */ }
}
