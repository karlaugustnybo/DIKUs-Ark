# IUCN downloads: instructions for future agents

Use this when asked to acquire IUCN files for this repository. Perform website
interaction with the agent's **browser/computer-control tools**. Previous
attempts to implement this interaction in pipeline scripts did not work;
do not start another scripted downloader. Scripts prepare the file inventory
and validate/register completed downloads.

## Prepare

1. Read [the acquisition guide](02_data_acquisition.md). Check `GLOBAL_DATA_ROOT`
   and available disk space; keep these large downloads on the intended data
   disk. Run `just download` if a current action plan is not already available.
   Exit code 2 means a download or authorization action remains.
2. Read `$GLOBAL_DATA_ROOT/acquisition/action-required.json`. Use its official
   pages, file URLs, destination directories, and configured release. Consult
   the inventory files referenced by `config/data_sources.toml` for file labels,
   formats and `provider_filename` values. The action plan can list every file
   in an incomplete source: skip files that are already present and valid.

## Operate the browser

3. Use an existing signed-in browser session when available. Inspect the live
   page before acting; do not assume old button labels or page layouts still
   apply. Check that the website's selected release matches the inventory.
   If it differs, report the mismatch instead of mixing releases.
4. Complete login and download forms using the account holder's supplied
   details and intended-use statement (`IUCN_USE_DESCRIPTION`, if configured).
   Reuse authorization already given in the conversation. Ask only for missing
   required information or user-only steps such as MFA/CAPTCHA. Never invent an
   intended use, expose credentials, or record authorization that was not given.
5. Set the browser's download destination to the planned directory, or move
   completed files there afterwards. Use computer control for native download
   dialogs when browser controls cannot reach them. Follow the official website
   download flow for each requested polygon, point, HydroBASINS table, or tabular
   export. Keep different formats and source directories distinct.
6. Track transfers in the browser's downloads view. Wait for completion;
   `.crdownload`/`.part` files and HTML login/error pages are not datasets.
   Match each completed transfer to its requested inventory entry, then rename
   it to the exact `provider_filename` if needed. Some table downloads have
   generic provider names: never infer their identity from download order alone.
   Keep ZIP archives intact and avoid overwriting an existing valid file.

## Verify and leave a useful handoff

7. After the account holder's authorization is established, record
   `IUCN_DATA_AUTHORIZED=true` in the ignored `.env`, if not already set.
   Rerun `just download` to validate and register the staged files in place.
   Resolve its reported missing files, invalid archives or schema errors; a
   completed browser transfer alone is not successful acquisition.
8. Report what was registered and what remains. For interrupted sessions, keep
   a short checkpoint at `$GLOBAL_DATA_ROOT/acquisition/browser-progress.md`
   with completed, in-progress and failed entries, their formats and destination
   filenames. Exclude credentials and session tokens. Restore any browser
   download preference changed solely for this task.

Stop after acquisition unless processing was also requested. `just data-build`
starts the expensive downstream workflow; `just download` does not.
