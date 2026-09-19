// electron-builder `afterPack` hook (docs/design/05-w6-interfaces.md §3.2):
// signs the bundled Python payload under `Contents/Resources/daemon` AND the
// whole `.app`, ad-hoc — with `mac.identity: null` (no Developer ID on this
// machine, docs/spikes/02-packaging.md §3), electron-builder's own signing
// path never runs at all (confirmed in the spike: app-builder-lib's
// macPackager.js returns before any signing when `identity` is null), so
// without this hook the packaged daemon binaries would ship completely
// unsigned. Reuses `packaging/sign/sign-adhoc.sh` (already spike-validated:
// by-hand signs everything under Resources/daemon by hand — codesign --deep
// does NOT reach that far — then signs the whole .app) rather than
// reimplementing the same by-inside-out codesign sequence here.
//
// `.cjs` (not `.mjs`): electron-builder's afterPack contract loads this via
// `require()`, and this repo's apps/desktop/package.json has
// `"type": "module"` — an `.mjs` extension here isn't the issue (this file
// lives under packaging/release/, which has no `package.json` `"type"`
// field of its own), but `.cjs` makes the CommonJS `exports.default =`
// shape unambiguous regardless.
const { execFileSync } = require('node:child_process')
const path = require('node:path')

// round-1 review fix (评审 #3): this hook used to run sign-adhoc.sh
// unconditionally, with no check of `mac.identity`. docs/release.md §3 step 2
// tells a releaser to swap `mac.identity: null` for a real "Developer ID
// Application" identity so electron-builder signs the .app/Frameworks/Helpers
// itself — but that alone does nothing about this hook, which would still run
// sign-adhoc.sh's ad-hoc-only parameters (`--sign -`, `--timestamp=none`)
// against Contents/Resources/daemon (extraResources — electron-builder's own
// signing never reaches it, confirmed in sign-adhoc.sh's own comments/spike
// 02) and, depending on hook ordering, possibly re-sign the WHOLE .app ad-hoc
// on top of electron-builder's real signature. Either way `notarytool submit`
// would only fail later, at the worst point to discover it. Real-identity
// signing of Resources/daemon (sign-adhoc.sh's own documented "正式发布所需
// 步骤" §3: drop --timestamp=none, use the real identity, no --deep) isn't
// implemented here — it's never been run against a real certificate — so this
// fails fast instead of silently shipping an ad-hoc-signed payload.
exports.default = async function afterPack(context) {
  const identity = context.packager.config?.mac?.identity
  const appName = `${context.packager.appInfo.productFilename}.app`
  const appPath = path.join(context.appOutDir, appName)

  if (identity) {
    throw new Error(
      `[afterPack] mac.identity is set ("${identity}") but afterPack.cjs only ` +
        'knows how to ad-hoc sign (packaging/sign/sign-adhoc.sh, --sign - ' +
        '--timestamp=none — never correct with a real identity). Signing ' +
        'Contents/Resources/daemon with a real Developer ID is documented but ' +
        'NOT implemented (see sign-adhoc.sh\'s "正式发布所需步骤" §3 and ' +
        'docs/release.md §3) — implement and verify it against a real ' +
        'certificate before packaging with mac.identity set, or revert to null ' +
        'for ad-hoc packaging.'
    )
  }

  const signScript = path.join(__dirname, '..', 'sign', 'sign-adhoc.sh')

  console.log(`[afterPack] ad-hoc signing ${appPath}`)
  execFileSync(signScript, [appPath], {
    cwd: path.dirname(signScript),
    stdio: 'inherit'
  })
}
