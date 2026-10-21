# UX pass: progress

The UX pass turns the eight tabs into a linear workflow. It keeps the tabs, the classic look and
every feature. Each phase is tested, committed and pushed on its own. A new session resumes at the
first unticked phase.

Work on a second UI instance (`ap ui --port 8701` or similar). Never restart the user's `ap ui`
(8700) or ComfyUI.

## Phases

- [x] 1. Toolbar: only project, New…, storage and Stop jobs. Stage buttons move into City / Site /
      Shots / Frames, and stay in the menus. "Update plan, greybox & passes" shows its steps first.
- [x] 2. Header strip on every tab: "This step: …", an honest status, and "Next: <tab> →" (disabled
      with a reason). Points back to an earlier tab that is out of date.
- [x] 3. The left panel is the same on every tab. The Frames shot list moves into the tab.
- [x] 4. The stage sidebar follows the tab order and uses the same states. Site and City are one
      stage, with a note.
- [x] 5. Frames: "Use this frame for this shot" (lock), a note that starring ≠ locking, then
      "Discover assets →".
- [ ] 6. Library opens on Areas (a sub-tab with its Next column).
- [ ] 7. Unreal tab as numbered steps; creates a look preset from the approved shot when none exists.
- [ ] 8. Empty states in Reference sheets and Views and 3D name the real prerequisite.
- [ ] 9. Versions: which version export/Unreal will use, next to Approve.
- [ ] 10. Labels (Frame crop, Description only, …); RUNBOOK.md and tests.
- [ ] 11. Base text 12–13px (secondary 11–12px), checked at 100% zoom.
- [ ] 12. Optional guide panel (Help › Show guide).
- [ ] Final: before/after screenshots of every tab in a private artifact.

## Notes

- Phase 1: toolbar buttons moved to City (Update plan, greybox & passes…, Update city plan, Rebuild
  greybox), Site (Update greybox & passes…) and Frames (Generate missing frames). Shots already had
  Render all passes. Apply opens a step list (applyPreview) before running anything.
- Phase 2: asset_pipeline/ui/static/workflow.js holds STEPS (tab order; Site and City are one step)
  and STEP_STATE (status, detail and what Next needs, from loaded data). renderStepHeads() is called
  at the end of renderShell(). Library "Out of date" = a locked frame not in the plan's
  `candidates` (not `sources`: frame_fit drops old-layout frames on purpose). Tests:
  tests/test_ui_workflow.py runs workflow.js in node with stubbed data.

- Before screenshots: taken from a worktree of b549843 on port 8701 (scratchpad, not in the repo).
