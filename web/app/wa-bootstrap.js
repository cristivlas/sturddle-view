// Configure Web Awesome to load from local paths only — no CDN.
// Must run before any <wa-*> element gets upgraded. We import components
// explicitly rather than relying on the auto-loader, which uses root-relative
// URLs that don't honor setBasePath in our setup.

import {
  setBasePath,
  setIconPath,
} from "../vendor/webawesome/webawesome.js";

setBasePath("/ui/vendor/webawesome/");
setIconPath("/ui/vendor/fontawesome-free/svgs");

// Components used in the app — registered eagerly so autoloader is unnecessary.
import "../vendor/webawesome/components/icon/icon.js";
import "../vendor/webawesome/components/dialog/dialog.js";
import "../vendor/webawesome/components/button/button.js";
import "../vendor/webawesome/components/input/input.js";
import "../vendor/webawesome/components/textarea/textarea.js";
import "../vendor/webawesome/components/radio/radio.js";
import "../vendor/webawesome/components/radio-group/radio-group.js";
import "../vendor/webawesome/components/switch/switch.js";
import "../vendor/webawesome/components/select/select.js";
import "../vendor/webawesome/components/slider/slider.js";
import "../vendor/webawesome/components/option/option.js";
import "../vendor/webawesome/components/tab-group/tab-group.js";
import "../vendor/webawesome/components/tab/tab.js";
import "../vendor/webawesome/components/tab-panel/tab-panel.js";
import "../vendor/webawesome/components/details/details.js";
