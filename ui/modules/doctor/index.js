// `#/doctor`, Data Doctor's old address (Plan L M151, DEC-1513). The one Data Doctor screen (Plan O M247, DEC-1817) is
// Data › Data Doctor (`#/data/check`), so this route only forwards there. Home links there through its "For you" line
// (`registerForYou`), so no shared screen is edited.

import { registerForYou } from "../../overview.js";
import { registerModule } from "../router.js";

const DOCTOR_HREF = "#/data/check";

registerModule({
  name: "doctor",
  routes: ["doctor"],
  render: async () => {
    window.location.replace(DOCTOR_HREF);
  },
});

registerForYou(() => ({
  text: "Not sure where to start? Bring your data to Data Doctor",
  href: DOCTOR_HREF,
}));
