#!/usr/bin/env bash
# 正式公证流程（需要 Apple Developer 账号 + 真实签名，本机未执行，仅作为可运行的文档）。
# 前置：已用 Developer ID Application 证书签名并打好 .dmg（见 sign-adhoc.sh 末尾步骤 1-3）。
set -euo pipefail

DMG="${1:?usage: notarize.sh <path.dmg> <keychain-profile-name>}"
PROFILE="${2:?usage: notarize.sh <path.dmg> <keychain-profile-name>}"

# 一次性设置（交互式，输入 App 专用密码，存入本机 Keychain，之后免密调用）：
#   xcrun notarytool store-credentials "$PROFILE" \
#     --apple-id <APPLE_ID_EMAIL> --team-id <TEAMID> --password <APP_SPECIFIC_PASSWORD>

xcrun notarytool submit "$DMG" --keychain-profile "$PROFILE" --wait

xcrun stapler staple "$DMG"

echo "--- 验证 Gatekeeper 会接受（离线也应通过，因为票据已 staple） ---"
spctl --assess --type open --context context:primary-signature --verbose=4 "$DMG"
