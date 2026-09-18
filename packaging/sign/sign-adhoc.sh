#!/usr/bin/env bash
# 本机没有 Apple Developer 证书，只能做 ad-hoc 签名（identity "-"）验证签名/校验流程本身。
# ad-hoc 签名不满足 Gatekeeper：没有 Developer ID，spctl 断网也好联网也好都会拒绝
# （"source=Unnotarized Developer" 都不会出现，因为根本没有开发者身份）。
#
# 本脚本全程 --timestamp=none 是 ad-hoc 阶段的正确选择（ad-hoc 身份本来就拿不到安全时间戳，
# 强行请求只会报错）。这不是「正式签名照抄本脚本、把 --sign - 换成 Developer ID」就够了——
# 正式签名必须去掉 --timestamp=none（notarytool 要求安全时间戳，否则直接拒绝："The signature
# does not include a secure timestamp"），且不应该依赖本脚本末尾那种 --deep 一把梭签整个 .app
# （Apple TN3125 明确 --deep 不该用于签名，只用于校验/调试；对 Electron 的 Frameworks/Helpers
# 这类嵌套 bundle，entitlements 不会正确按每个 helper 的需要下发）。正式发布的推荐路径见
# 本文件末尾「正式发布所需步骤」，与 electron-builder.yml 的说明。
# 公证流程见 notarize.sh。
set -euo pipefail
APP="${1:?usage: sign-adhoc.sh <path-to-.app>}"

# 先签内层：PyInstaller bundle 里所有 .dylib / .so / 可执行文件，从最深层开始，
# 否则外层签名会覆盖内层签名的校验和（Apple 文档要求由内向外签）。
#
# codesign 的退出码必须原样保留——不能通过管道丢给 grep 再 `|| true`（DEV.md 工程原则 4：
# 诚实失败）。这里用 `out=$(...)` 直接拿 codesign 自己的退出码，`grep -v` 只用来过滤展示，
# 不参与成败判断，所以它自己的 `|| true` 是安全的（找不到匹配行也不该让脚本失败）。
find "$APP/Contents/Resources/daemon" -type f \( -name "*.dylib" -o -name "*.so" -o -perm -u+x \) -print0 2>/dev/null | \
  while IFS= read -r -d '' f; do
    if ! out=$(codesign --force --sign - --timestamp=none "$f" 2>&1); then
      echo "$out" >&2
      echo "codesign failed for: $f" >&2
      exit 1
    fi
    echo "$out" | grep -v "replacing existing signature" || true
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
# 2. 签 .app 本体、Frameworks、Helpers：**不要**照搬本脚本第 24 行那种 `--deep` 一把梭。
#    正确路径是把 electron-builder.yml 的 `mac.identity` 从 null 换成
#      "Developer ID Application: <公司/个人名> (<TEAMID>)"
#    让 electron-builder（底层调用 @electron/osx-sign）按 Electron 官方推荐的顺序逐个签
#    Helper.app / Helper (GPU).app / Helper (Renderer).app / Helper (Plugin).app / 各 Framework
#    / 最后主 .app，且各自的 entitlements 正确下发（这条路径本 spike 未实测，见
#    electron-builder.yml 里的说明与 docs/spikes/02-packaging.md §3；identity: null 时这整段
#    从未执行过，见 app-builder-lib 的 macPackager.js）。
#
# 3. 单独签 Resources/daemon：extraResources 不是 electron-builder 认识的「代码」位置，
#    上一步不会碰它，仍然需要本脚本第 9-20 行这种「由内向外遍历 .dylib/.so/可执行文件」的
#    手工签名，但要做两处改动（否则会在 notarytool 阶段才失败，把坑推迟到发布时）：
#      a. 去掉 `--timestamp=none`，改用默认的 `--timestamp`（安全时间戳）——
#         notarytool 要求所有签名都带安全时间戳，没有会直接拒绝。
#      b. `--sign -` 换成 `--sign "Developer ID Application: <公司/个人名> (<TEAMID>)"`；
#         不再需要 `disable-library-validation`（正式发布前把这些 .dylib 也用同一个
#         Team ID 重签，而不是靠禁用库校验绕过，entitlements.mac.plist 里那条也应删掉）。
#
# 4. 打 .dmg（electron-builder 的 dmg target 会自动做，或手动 hdiutil create）。
#
# 5. 公证（notarize.sh 里的完整命令）：
#      xcrun notarytool submit JonesPackagingSpike.dmg \
#        --apple-id <APPLE_ID_EMAIL> --team-id <TEAMID> --password <APP_SPECIFIC_PWD> \
#        --wait
#    需要先用 `xcrun notarytool store-credentials` 把 App 专用密码存进 Keychain，
#    避免明文密码出现在命令行历史（对应本仓库"凭据不落明文"的原则）。
#
# 6. Staple 公证票据（离线也能通过 Gatekeeper）：
#      xcrun stapler staple JonesPackagingSpike.dmg
#
# 7. 验证：
#      spctl --assess --type open --context context:primary-signature -v JonesPackagingSpike.dmg
#      应输出 "accepted, source=Notarized Developer ID"
#
# 以上第 2-3 步在本机无付费开发者账号的情况下完全没有执行过一次，是否真的能通过 notarytool
# 扫描仍未验证——见报告「没做什么」一节与 Issue #2 后续追踪。
# ============================================================================
