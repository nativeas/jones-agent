#!/usr/bin/env bash
# 本机没有 Apple Developer 证书，只能做 ad-hoc 签名（identity "-"）验证签名/校验流程本身。
# ad-hoc 签名不满足 Gatekeeper：没有 Developer ID，spctl 断网也好联网也好都会拒绝
# （"source=Unnotarized Developer" 都不会出现，因为根本没有开发者身份）。
# 正式签名 + 公证流程见本文件末尾注释与 notarize.sh。
set -euo pipefail
APP="${1:?usage: sign-adhoc.sh <path-to-.app>}"

# 先签内层：PyInstaller bundle 里所有 .dylib / .so / 可执行文件，从最深层开始，
# 否则外层签名会覆盖内层签名的校验和（Apple 文档要求由内向外签）。
find "$APP/Contents/Resources/daemon" -type f \( -name "*.dylib" -o -name "*.so" -o -perm -u+x \) -print0 2>/dev/null | \
  while IFS= read -r -d '' f; do
    codesign --force --sign - --timestamp=none "$f" 2>&1 | grep -v "replacing existing signature" || true
  done

# 再签 daemon 的 Python 解释器本体（python-build-standalone 方案：Resources/daemon/python/bin/python3.12）
DAEMON_EXE="$APP/Contents/Resources/daemon/python/bin/python3.12"
if [ -f "$DAEMON_EXE" ]; then
  codesign --force --sign - --timestamp=none --entitlements ../electron-shell/entitlements.mac.plist --options runtime "$DAEMON_EXE"
fi

# 最后签整个 .app（electron-builder 在无 identity 时通常已经做了 ad-hoc 签名，这里是
# 手工验证同一流程；正式发布时这一步由 electron-builder + electron-notarize 自动完成）。
codesign --force --deep --sign - --timestamp=none --entitlements ../electron-shell/entitlements.mac.plist --options runtime "$APP"

echo "--- codesign --verify ---"
codesign --verify --deep --strict --verbose=2 "$APP"

echo "--- spctl assessment (预期：ad-hoc 签名会被拒，这是预期行为，不是 bug) ---"
spctl --assess --type execute --verbose=4 "$APP" || echo "(预期失败：ad-hoc 签名不是 Gatekeeper 信任的 Developer ID，需要正式签名+公证)"

# ============================================================================
# 正式发布所需步骤（本机无证书，未执行，留档）：
#
# 1. 注册 Apple Developer Program（$99/年），在 developer.apple.com 生成：
#    - "Developer ID Application" 证书（给 .app / 其内部可执行文件签名）
#    - 该证书导入本机 Keychain，`security find-identity -v -p codesigning` 能看到
#
# 2. 用真实身份重复上面的签名步骤，把 `--sign -` 换成：
#      --sign "Developer ID Application: <公司/个人名> (<TEAMID>)"
#    并且不再需要 disable-library-validation（正式发布前应把 PyInstaller 打出的
#    所有 .dylib 也用同一个 Team ID 重签，而不是靠禁用库校验绕过）。
#
# 3. 打 .dmg（electron-builder 的 dmg target 会自动做，或手动 hdiutil create）。
#
# 4. 公证（notarize.sh 里的完整命令）：
#      xcrun notarytool submit JonesPackagingSpike.dmg \
#        --apple-id <APPLE_ID_EMAIL> --team-id <TEAMID> --password <APP_SPECIFIC_PWD> \
#        --wait
#    需要先用 `xcrun notarytool store-credentials` 把 App 专用密码存进 Keychain，
#    避免明文密码出现在命令行历史（对应本仓库"凭据不落明文"的原则）。
#
# 5. Staple 公证票据（离线也能通过 Gatekeeper）：
#      xcrun stapler staple JonesPackagingSpike.dmg
#
# 6. 验证：
#      spctl --assess --type open --context context:primary-signature -v JonesPackagingSpike.dmg
#      应输出 "accepted, source=Notarized Developer ID"
# ============================================================================
