// Data Doctor's hand-off to Guided setup (Plan L M151, DEC-1513): the upload `POST /doctor/handoff`
// made for a use case waits here until that use case's Guided setup mounts and takes it, so the
// person lands on the helper's checklist with nothing to upload again. In memory only: a reload
// forgets it, and the person simply picks the file again.

const pending = new Map();

const keyOf = (useCaseId, mode) => `${useCaseId}|${mode}`;

/** Leave `upload` (what `POST /uploads` answers) for the use case's Guided setup in `mode`. */
export function handOff(useCaseId, mode, upload, fileName) {
  pending.set(keyOf(useCaseId, mode), { upload, fileName });
}

/** The upload left for this use case and mode, once; `null` when there is none. */
export function takeHandoff(useCaseId, mode) {
  const key = keyOf(useCaseId, mode);
  const found = pending.get(key) || null;
  pending.delete(key);
  return found;
}
