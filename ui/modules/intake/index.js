// Plan L M150 (DEC-1505): the intake module's entry point, loaded by `ui/index.html`'s PLAN-L block. It lends the
// Setup form its upload notes through the router (`registerUploadNotes`), so no Phase 1 file imports it.

import { registerUploadNotes } from "../router.js";
import { TABLE_FILE_TYPES, bindIntakeNotes, intakeNotesHtml } from "./notes.js";

registerUploadNotes({ accept: TABLE_FILE_TYPES, html: intakeNotesHtml, bind: bindIntakeNotes });
