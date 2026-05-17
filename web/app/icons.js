// Custom inline SVG icons not covered by the Font Awesome Free set used
// elsewhere. Values are the inner <svg> markup only -- callers wrap via
// inlineSvgIcon() from dialogs.js. Hand-composed from primitives; close
// in spirit to the reference, not pixel-faithful.

export const CHESS_CLOCK_SVG_INNER = `
  <!-- Top plunger (left) and button (right) -->
  <rect x="80"  y="40" width="100" height="36" rx="18" ry="18"/>
  <rect x="330" y="40" width="90"  height="32" rx="16" ry="16"/>
  <!-- Stems connecting tops to body -->
  <rect x="115" y="70" width="30" height="36"/>
  <rect x="355" y="68" width="40" height="38"/>
  <!-- Body -->
  <rect x="48" y="104" width="416" height="288" rx="40" ry="40"/>
  <!-- Clock faces (cut out from body via white fill) -->
  <circle cx="170" cy="248" r="92" fill="#fff"/>
  <circle cx="342" cy="248" r="92" fill="#fff"/>
  <!-- Left hand: ~7 o'clock direction -->
  <rect x="125" y="240" width="60" height="22" rx="11" ry="11"
        transform="rotate(35 155 251)" fill="currentColor"/>
  <!-- Right hand: ~2 o'clock direction -->
  <rect x="330" y="200" width="22" height="80" rx="11" ry="11"
        fill="currentColor"/>
`;
