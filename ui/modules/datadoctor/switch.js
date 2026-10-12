// Whether the one Data Doctor screen is on here (Plan O M247, DEC-1817).
//
// `GET /auth/me` - the call the UI already makes at start-up - carries `data_doctor: true` while the deployment's
// `data_doctor.unified` switch is on; the field is absent while it is off (so that answer is byte for byte what it
// was). Asked of the production session module (`../production/session.js`), never with a request of its own: with
// the switch off nothing here sends anything, and every screen draws exactly as it did.
//
// `dataDoctorOnNow()` is the answer known now (false until `/auth/me` answered); `dataDoctorOn()` waits for it.
// `onDataDoctor(listener)` is called once when the answer turns on, so a screen drawn before it can draw again.

import { currentMe, ensureMe, onSession } from "../production/session.js";

let on = false;
const listeners = new Set();

const read = (me) => Boolean(me && me.data_doctor === true);

onSession((me) => {
  const next = read(me);
  if (next === on) return;
  on = next;
  if (on) for (const listener of [...listeners]) listener();
});

/** True once `/auth/me` said the screen is on; false before it answered and while the switch is off. */
export const dataDoctorOnNow = () => on || read(currentMe());

/** Resolves to whether the screen is on, once `/auth/me` has answered (false when it cannot be asked). */
export async function dataDoctorOn() {
  if (dataDoctorOnNow()) return true;
  try {
    return read(await ensureMe());
  } catch {
    return false;
  }
}

/** Call `listener` when the answer turns on; returns a function that stops listening. */
export function onDataDoctor(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
