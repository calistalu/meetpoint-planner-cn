# Preference Mapping (CN)

Use this file to normalize natural-language user preferences into search keywords for AMap POI APIs.

## Built-in mapping

- 猫咖 -> 猫咖, 猫咪咖啡, 撸猫
- 按摩 -> 按摩, 推拿, SPA
- 咖啡 -> 咖啡, 咖啡馆
- 甜品 -> 甜品, 蛋糕
- 火锅 -> 火锅
- 桌游 -> 桌游, 棋牌
- 电影 -> 电影院, 影城
- 商场 -> 购物中心, 商场

## Rules

- Keep original user words and append mapped synonyms.
- If user gives multiple intents, split by spaces, commas, `、`, `/`, and `|`.
- If no intent is given, default to `咖啡` and `餐厅`.
- If results are sparse, rerun with broader words (example: `猫咖` -> `咖啡馆`).

## Chat-to-script examples

- User: "想找中间位置的猫咖，地铁方便"
- Preference argument: `猫咖 地铁`

- User: "想按个摩再吃点甜品"
- Preference argument: `按摩 甜品`
