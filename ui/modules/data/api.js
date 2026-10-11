// The Data page's one new call (Plan M, M179): the uploaded files. Everything else the page does (list, test,
// delete) is the Connections module's own functions in `../connections/api.js`.

import { call } from "../connections/api.js";

/** `{ uploads: [{ upload_id, file_name, file_format, file_size_bytes, row_count, column_count, use_case_id,
 *  mode, synthetic, created_at, from_connection_id }] }`, newest first. */
export const getUploads = (limit = 100) => call(`/uploads?limit=${encodeURIComponent(limit)}`);
