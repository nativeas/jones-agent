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

exports.default = async function afterPack(context) {
  const appName = `${context.packager.appInfo.productFilename}.app`
  const appPath = path.join(context.appOutDir, appName)
  const signScript = path.join(__dirname, '..', 'sign', 'sign-adhoc.sh')

  console.log(`[afterPack] ad-hoc signing ${appPath}`)
  execFileSync(signScript, [appPath], {
    cwd: path.dirname(signScript),
    stdio: 'inherit'
  })
}
