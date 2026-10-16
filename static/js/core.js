const ALL_CONTROLS = CONTROLS.concat(GLOBAL_CONTROLS, COLOR_CONTROLS, M8_CONTROLS);
const $ = (id) => document.getElementById(id);

let current = null;      // relative path of the open file
let values = {};         // live slider values
let neutral = {};        // values representing "no edits"
let renderToken = 0;     // guards against out-of-order preview responses
let files = [];          // library paths, in display order
let selected = new Set();// paths ticked for a batch apply
let lastIndex = -1;      // anchor for shift-click ranges
let comparing = false;   // true while the compare key is held
let editedSrc = null;    // cached edited preview, restored when compare ends
let clipboard = null;    // settings copied from one photo, for pasting onto others
let baselineTemp = null; // this photo's own camera white balance
let cropping = false;    // crop overlay visible
let importValues = null; // as-imported (auto) settings: what 'Before' shows
let histState = { entries: [], index: -1, snapshots: [] };
let baMode = "off";     // off | split | side
let baSplit = 50;        // divider position, percent
let beforeSrc = null;    // cached render of importValues for this photo
let cropRatio = null;    // locked aspect ratio, or null for free
