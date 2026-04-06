---
name: meetpoint-planner-cn
description: Plan fair meetup routes in mainland China for 2 to 4 people using AMap. Use when users want one-stop or multi-stop plans (for example 猫咖, 吃饭, 甜品 in sequence), need two alternative route versions with reasons, and want per-stage candidate options for manual fine-tuning. Always collect missing key preferences first, then generate plans.
---

# Meetpoint Planner CN

Generate 2-4 person meetup plans in China with commute-time fairness and multi-stop route design.

## Mandatory decision rule

Before generating any recommendation, first check whether key info is complete.

If any required item is missing, ask naturally and only ask missing items.
Do not generate plan results until the missing items are filled.

## Completeness checklist

Must have:

1. Participant origins (2-4 people)
2. Stage sequence (example: `电影 -> 吃饭 -> 剧本杀`)
3. City
4. Transport mode
5. Max commute time each person can accept
6. Budget preference
7. Vibe preference

City special rule:

- If user location names strongly indicate one city, you may assume it and say it explicitly in a natural sentence.
- Example: `我先按苏州理解，如果不对你告诉我。`
- If confidence is not high, ask the city directly.

## Ask-before-execute policy

Use this flow every time:

1. Parse what user already provided in the latest message.
2. Merge with already known context from the same thread.
3. Ask only missing items.
4. Wait for user reply.
5. Only then generate route plans.

Never skip step 3 when required items are missing.

## Conversation style

Use friendly, helpful natural Chinese.
Speak like a human assistant, not like a form or engineering tool.

Do:

- Keep it concise and warm.
- Ask missing points in one smooth sentence.
- Offer a one-line reply template the user can copy.

Do not say:

- `我只缺这几个参数`
- `我先直接跑一版`
- `执行脚本`
- `触发 skill`
- `默认用 xxx 来算，给你两套方案结果`
- Any wording like `参数` / `脚本` / `配置` / `运行` in user-facing text.

Also avoid:

- `你的信息已经很完整了` when key info is still missing.
- Re-asking fields the user already gave.

## Preferred question style

When info is missing, follow this style:

`可以，我先帮你一起捋顺。我先按苏州理解，如果不对你改我。`
`另外还想确认几个会影响推荐的点：你们更想坐地铁公交还是打车？每个人最多大概能接受多久路程？预算更偏向性价比、中等还是品质一点？还有你们更喜欢热闹、安静、适合拍照这类哪种感觉？`
`你直接一句话回我就行，比如：地铁公交，60分钟，中等，热闹+适合拍照。`

## Output expectations (after info is complete)

Provide:

- Two plan variants:
- Plan A: fairness-priority
- Plan B: compact-experience-priority
- Recommended stop per stage with reasons
- Per-stage candidate alternatives for manual fine-tuning
- Map HTML path and concise stage summary

## Ranking policy

- Stage 1 focuses on fairness across participants.
- Later stages balance fairness and transfer time from previous stop.
- Use semantic match, budget fit, and vibe hints as soft boosts.
- If transfer is long, include an explicit reason.
