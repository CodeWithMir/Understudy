# Learned skills

Understudy writes a folder here for every tool/target where it hit an error:

    <framework>--<target host>/SKILL.md      lessons about the target application (selectors, waits, notes)
    <framework>--runtime/SKILL.md            lessons about running the tool on this machine (browser, driver, packages)
    .../lessons.json                         the same lessons, machine-readable

A lesson starts as *unresolved* when a run fails and becomes *learned* when self-healing fixes it. Learned fixes are
applied automatically before the next run with the same tool or target, so the same error doesn't happen again.
Manage them in Mission Control > Learned skills. Deleting a folder makes Understudy forget those lessons.
The folder can be moved with `UNDERSTUDY_SKILLS_DIR`.
