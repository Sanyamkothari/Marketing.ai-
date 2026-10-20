// The Data page's one new call (Plan M, M179): the uploaded files. Everything else the page does (list, test,
// delete) is the Connections module's own functions in `../connections/api.js`.

import { call } from "../connections/api.js";

/** `{ uploads: [{ upload_id, file_name, file_format, file_size_bytes, row_count, column_count, use_case_id,
 *  mode, synthetic, created_at, from_connection_id, made_by, source_upload_id }], total }`, newest first. `total` counts every upload,
 *  so a list cut at `limit` can say so. `made_by` is "file", "import", "pull", "renamed_copy" or "recipe".
 *  `source_upload_id` is, for a "recipe" copy, the upload it was prepared from (else null). */
export const getUploads = (limit = 100) => call(`/uploads?limit=${encodeURIComponent(limit)}`);
