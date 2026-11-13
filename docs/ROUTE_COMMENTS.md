# Route Comments

Route Comments keeps spatial comments on cable routes and other project features: review remarks, survey observations, open questions. It is a register, not a workflow. There are no roles, approvals, accounts or locking, and no transitions are enforced between statuses.

## Adding a comment

**From the dock.** Click **Add** (left of the search box) and click the map. The KP Mouse Tool does not need to be on: KPs are measured on its reference line, with its start KP and the plugin-wide KP setting, exactly as the KP Mouse Tool measures them. While you move the mouse, the route position a click would use is marked and its KP shown in the status bar.

- **Click**: a comment on the route at the nearest KP.
- **Shift+click**: a comment exactly where you click; its nearest KP and distance off the route are kept apart.
- **Drag along the route**: a comment over a KP range, previewed while you drag.
- **Esc**, a right-click or choosing another map tool stops; the map tool you had before comes back.

Add stays on after each comment (like QGIS's own digitising tools), so you can add several in a row. Without a reference line, a click places a comment at that map position, without a KP. The arrow beside **Add** offers more:

- **At a KP…** / **Over a KP range…**: the new-comment form starts at the route position nearest the map centre; type the KP or range (previewed on the map).
- **General comment (no location)…**: a remark on the whole route or document. It is listed in the table, with the type *General*, but not drawn on the map. To give it a location later, right-click it in the table and choose **Place on the map…** (or double-click its KP cell), then click the map.

**From the KP Mouse Tool.** With the **KP Mouse Tool** on, right-click the map and choose **Add Comment**:

- **At Nearest Route Point (KP …)** places the comment on the route at the KP shown.
- **At Cursor Location** keeps the position you clicked. Its nearest KP and distance off the route are recorded separately, so the actual position is never moved onto the route.
- **Over KP Range…** fixes the start at the nearest KP. Move along the route (the section is previewed) and click the end. **Esc** or a right-click cancels.

A short form opens with the author, today's date, the first status and the default priority already filled in, plus the location (KP, KP range, or latitude/longitude), which you can edit. The proposed position is previewed on the map as you change it; **Zoom to preview** brings it into view. Type the comment and press **Ctrl+Enter**. Tags and the set's custom fields are under **More fields**.

The KP Mouse Tool's other right-click entries are unchanged.

## The dock

**Plugins ▸ Subsea Cable Tools ▸ Route Comments** (also in the toolbar's *Experimental* dropdown). Drag its title bar off the QGIS window to use it as a separate window with minimise and maximise buttons; it reopens the way you left it.

- **Register button**: create a register, **duplicate** it (a copy under a new name, with its images; you are asked whether this project should use the copy), open one, recent registers, show the folder.
- **Set combo / Set menu**: switch comment sets. You can also create, **duplicate**, rename or delete a set, **renumber it by KP**, edit its custom fields (text, number, date, yes/no, choice), and edit its status and priority choices. Statuses marked *counts as closed* are drawn grey and can be hidden. See [Copying, moving and renumbering](#copying-moving-and-renumbering).
- **Search** matches every word typed against the text, replies, number (`#12`), author, tags, route and custom values. **Filter** narrows by status, priority, reference condition and type.
- **Table** (across the top): click a header to sort. Right-click a header to show or hide columns or reset them. Column order, widths, visibility and sort are remembered. Double-click (or F2) the comment, status, priority, author, date, tags, external ID or a custom field to edit it in place; it is saved straight away, with the same check against changes made elsewhere as **Save**. Double-click a number to zoom to it. Double-click a KP (or press F2) to edit a route point or range, with a confirmation before it moves. A map location's **~** nearest KP is calculated from its position: double-click it to edit its coordinates. Right-click rows to zoom to them, set their status or priority, **copy or move them to another set**, copy their IDs or delete them.

Below the table is the selected comment. Drag the bar between them to share the height.

- **Header**: the comment's number and location, its **status** and **priority** (a change is saved at once), the up and down arrows (**Alt+Up** / **Alt+Down**) to step through the comments in the table's order, and **Follow on the map**, which pans the map to each comment you select here when it is out of view.
- **Thread**: the comment and its replies as a conversation, oldest first, with your own replies on the right. Type a reply and press **Send** (Ctrl+Enter). **Send & close** sends the reply (if you typed one) and sets the set's first closed status. On a closed comment the same button is **Send & reopen**. The thread shows each status change. Each message has a **⋯** button (or right-click it) to copy, edit or delete it. **Edit** opens the text in place: **Save** (Ctrl+Enter) or **Cancel** (Esc). An edited reply is marked *edited*. Editing a reply that changed the status keeps that change. Editing the comment itself works like editing its text in the table. If you move to another comment with an edit still open, you are asked whether to save it. Deleting the comment from the thread deletes it with its replies and images, as **Delete…** does. Deleting a reply that changed the status (*Send & close*) asks whether to put the status back. That is offered while the status is still the one the reply set and no later reply has changed it.
- **Details**: every other field of the comment. Edit it and **Save** (Ctrl+S).
- **Images**: the comment's images, beside the thread when the dock is wide and under it when it is narrow. The pane is small while the comment has no image. Zoom with the mouse wheel, drag to pan, and right-click to fit or show the image at actual size. Double-click opens it in your image viewer. When there are several images, the thumbnails and arrows switch between them. Add images with **Add…**, by dropping image files on the pane, or with **Paste** (**Ctrl+V** anywhere outside a text box), for example a screenshot. **Show on map** puts the image in the comment's map label (see below).

## Copying, moving and renumbering

**Copy or move comments to another set.** Select one or more comments in the table, right-click and choose **Copy … to set** or **Move … to set**, then a set, or **New set…** (it gets the current set's statuses, priorities and custom fields).

- A **copy** is a new comment with a new ID, with copies of the replies and images (the image files are copied, so each set has its own). The original stays where it is.
- A **move** keeps the comment's ID, replies and images; it is asked first. Other tools that refer to the comment by its ID (Span & Slack Analysis) still find it.
- In both cases the comments are numbered after the last comment of the target set, in their current order, and keep their text, status, priority, position, route reference and dates. Custom fields are matched by name; a field the target set lacks is added to it when the comments have values in it, so nothing is hidden. Statuses or priorities the target set does not list are kept as they are.
- A message above the table gives the new numbers, with a link to show that set.

**Duplicate a set** (*Set ▸ Duplicate set…*): everything (its comments with the same numbers and new IDs, with or without their replies and images), or the set-up only (statuses, priorities and custom fields, no comments), for example to start the next review round. The dock then shows the copy, with a link back to the original.

**Duplicate the register** (*Register ▸ Duplicate register…*): a consistent copy of the GeoPackage and its attachments folder under a new name, made even while others have the register open. An existing file is never overwritten. You can use the copy in this project (the comment layers are pointed at it) or keep the original; the copy is then in *Recent registers*.

**Renumber by KP** (*Set ▸ Renumber by KP…*): numbers the set's comments 1, 2, 3… in KP order, for example after a long back-and-forth review and before the comments are sent out.

- A KP range is ordered by its lower KP and a map location by its nearest KP; comments with equal KPs keep their order. Comments on different routes are numbered route by route. Comments without a KP (general comments, positions away from any route) come last.
- The whole set is renumbered, whatever the search or filters show. A preview lists the current and new numbers, and the change has to be confirmed (**Cancel** is the default).
- **Numbers quoted elsewhere** (in replies, e-mails, exported spreadsheets or reports) **no longer match**, and renumbering cannot be undone. By default each comment's number before renumbering is kept in a custom field **Previous No.** (added to the set when needed, and shown as a column afterwards). Consider *Duplicate set…* or *Export* first.
- New comments are numbered after the highest number, as before.

## Editing a position

KPs are shown in kilometres to **3 decimal places (0.001 km = 1 m)**. Stored positions keep their original precision unless you change them.

While typing in a KP cell or changing a position in a dialog, a **blue overlay** previews the unsaved position: a ring for a point, or a dashed section with rings at both range ends. Invalid or incomplete input hides the preview. The saved comment stays at its current position until you confirm or save; cancelling clears the preview. The new-comment and position dialogs include **Zoom to preview** (centres a point at the current scale, or fits the whole range). Typing does not pan or zoom the map automatically. Move the dialog aside if it covers the preview.

For a quick KP edit, **double-click the KP cell** or select it and press **F2**:

- A route point takes one KP, for example `12.345`.
- A range takes both ends, for example `12.345 – 13.456`. A hyphen or `to` also works, including with negative KPs.
- Press **Enter** or leave the cell to review the proposed move. The confirmation shows the old and new KPs; choose **Yes** to update the position and map, or **No** to keep it. **Esc** cancels typing. An unchanged value needs no confirmation and does not move the comment.
- Text, missing range ends, extra decimal places, non-finite numbers, zero-length ranges and KPs outside the route are rejected. Reversed range ends are put in route order before confirmation. You can use the route limits shown in the position dialog.
- A map location's **~** nearest KP is calculated from its coordinates and cannot be typed over. Double-click it to open the coordinate editor instead.

For the full position form, select a comment and choose **Details ▸ Edit position…**, or right-click its table row and choose **Edit position…**:

- **Route point:** change the KP to move the comment along its route.
- **KP range:** change either end; the section follows the route between those KPs. Reversed endpoints are put in route order. A zero-length range or a KP outside the route cannot be saved.
- **Map location:** change latitude or longitude. The comment keeps that position and its nearest KP and distance off route update automatically. Locations imported without a route can also be moved by coordinates.

A general comment has no position to edit: **Edit position…** reads **Place on the map…** for it and gives it one by a click on the map, after asking. These editing rules apply whether the comment came from the KP Mouse Tool, the dock's Add button, the Span & Slack Analysis profile or a CSV/Excel import. New comments keep the exact captured point, range endpoints or map coordinates when you accept their displayed values without changing them. Editing text, replies, status, priority, images or custom fields leaves the position alone.

**Save position** updates the geometry, table, map and labels together. **Cancel** leaves the saved position unchanged. Opening the form without changing a value preserves its full stored precision, including an untouched end of a range. Replies, images, IDs and other comment fields are kept.

Position edits use the comment's saved start KP, measurement convention and route revision, even if the current KP settings differ. The original route must be available and its geometry unchanged; restore it first if it has changed or is missing. A newer Workbench revision is not substituted. A concurrent change to the comment is reported without overwriting it; reload before trying again. Existing registers need no schema changes for position editing.

## On the map

The current set is drawn in a **Route comments** layer group, with points and map locations in one layer and KP ranges in another (general comments have no location and are not drawn). Both layers read the register directly, filtered to the set: there is no copy of the comments to refill or clean up, and they are saved with the project like any other layer. Open comments are amber, closed ones grey, and comments whose route reference has changed or is unavailable are red. A KP range is a wide translucent band with a dark centre line, so it shows over a route line. Points that crowd together are clustered.

**Labels** grow as you zoom in, so the map stays readable at every scale:

- from 1:2 000 000, the comment number only (`#12`);
- from 1:250 000, the number and the start of the comment (24 characters);
- from 1:50 000, the details you choose (by default the number and the first 40 characters of the comment) and the comment's map image, in a small white tag.

Each step includes its own scale: a map set to exactly 1:50 000 shows the details and images. (Before this version a map set to the step's scale could show the step before it; such labels are updated when the dock next opens, keeping your other label changes.)

**Label contents…** (map menu in the dock) picks the details: number, KP or KP range, status, priority, author, date, tags and the shortened comment, separated by spaces, middle dots or new lines. You can also write any QGIS expression (with the expression builder). The same dialog sets the scale of each step (or leaves a step out), the text size, the tag and the width of images in the tags; a step whose scale is not further out than the next one's is not shown, and the dialog says so. **Restore Defaults** puts them all back to the tool's defaults; labels set up before these steps existed keep their own two steps (number, then details) until you do. Where labels crowd, QGIS leaves some out, giving way to comments that need attention first, then open ones. **Labels** in the same menu turns them off.

**Images in labels.** A comment can show one of its images at the top of its detail tag, with the details underneath and the leader still pointing at the comment: tick **Show on map** under the image in the dock. It shows from the details step (1:50 000 by default), at the width set in **Label contents…** (30 mm by default, the height following the image). Each comment shows at most one image; untick it, or remove the image, to take it off the map. If the labels cannot show images (made before images could be shown, images turned off in **Label contents…**, or replaced in Layer Properties), you are asked to update them when you first tick **Show on map**, and after an import that puts images on the map. If the map is zoomed out further than the details step, a message says so, with a **Zoom in** button. The comment's text is always shown as plain text.

The label finds the image in the attachments folder beside the file the comment layer reads, as QGIS resolved that file when the project opened. So images keep showing when the project and register are opened from another place: another drive letter or share, or a folder synced to each user's own computer under their own user name, even before the Route Comments dock is opened there. (Labels made by earlier versions read the folder from the project variable `@route_comments_attachments`, which holds the full path on the computer that last set it; they are switched over when the dock next opens, changing nothing else in the labels. The variable is still kept up to date for your own styles.)

**Your own styling.** The plugin sets a default style once, when it creates the layers, and never changes it afterwards. Change symbols, colours and labels in Layer Properties as for any layer, and they are saved with the project. A few things to know:

- **Label contents…** replaces the layers' labels when you press OK.
- If you delete a comment layer, it comes back with the default style.
- Which set is shown is the layers' filter. Switching set changes the filter, so leave it to the dock.
- The layers are read-only, so comments are always changed through the dock, which checks for changes made elsewhere and keeps their timestamps.
- The default style colours comments using two project variables the dock keeps up to date: `@route_comments_closed` (the closed statuses) and `@route_comments_attention` (comments whose route changed or is unavailable). Your own style can use them too.

Selecting comments in the table selects them on the map and outlines them in a highlight colour with a white edge (**Highlight colour…** in the map menu changes it). Selecting features on these layers with any QGIS selection tool selects the comments in the table. **Pick comments on the map** (the identify button in the dock) lists every comment under the cursor, including ones hidden in a cluster or overlapping, so each one can be reached.

If the register file is moved, QGIS asks for it when the project opens, as for any missing layer. You can also open the register from its new place in the dock, and the layers are pointed at it.

## Route references

A comment made on a route keeps what it was made against:

- the route layer's ID, name, source, provider and CRS;
- for a Cable Route Workbench RPL, the RPL ID, name, revision, route and source file;
- a fingerprint of the route geometry, the route length, the start KP and the KP convention (geodesic WGS84, or grid with the grid CRS);
- the geometry, in EPSG:4326 and exactly as captured in the CRS it was captured in.

KPs come from the same route model as the KP Mouse Tool. That model uses the route order, the RPL's start KP when it is not zero (or the start KP set in the KP Mouse Tool) and the plugin-wide KP setting ([KP method](KP_METHOD.md)).

Each comment shows a **Reference** condition:

| Condition | Meaning |
|---|---|
| OK | The route is found and unchanged. |
| Newer revision | Unchanged, but the Workbench has a newer revision of this route. |
| Route changed | The route geometry, start KP or KP convention differs from when the comment was made. |
| Route unavailable | The route is not in the project and its source cannot be opened. |
| No route | The comment has no route reference (e.g. imported by position only). |

A route is looked up by layer ID, then by source, then by Workbench RPL, then by opening its file. A project layer with the comment's layer ID is its route when its source matches the one saved, or when the saved file is not there on this computer and the project layer reads a file of the same name and layer: the same project and data opened from another folder, such as a synced folder under another user name. The geometry, start KP and convention are still compared as usual. A comment is **never** recomputed against a different or changed route. It keeps the KPs and position it was made with, and the condition says why they may no longer match. Conditions are re-checked when the register reloads; use **Check route references now** after editing a route. Moving comments to a new revision is not part of this version.

## Storage

A register is a GeoPackage you choose (by default `<project>_comments.gpkg` beside the project), with images copied into `<name>_attachments/` beside it. Keep the two together when moving or sharing a register. Images are recorded by their file name only, never by a full path, and the project saves the register's path relative to the project (with QGIS's default relative paths), so a project and register in a shared or synced folder work for every user whatever the folder is called on their computer. The project remembers its register and set, and recent registers are listed in the register menu. Several projects, or several QGIS sessions, can open the same register. The register uses SQLite's standard (rollback) journal rather than a write-ahead log, so any program, QGIS included, can read it without the side files a write-ahead log needs. The dock picks up changes saved elsewhere every few seconds.

- Tables: `rc_set`, `rc_field`, `rc_comment` (a GeoPackage feature layer that QGIS can open directly), `rc_reply`, `rc_attachment`, `rc_meta`. Newer versions only add columns, such as the image shown on the map and the status a reply set. They are added when the register is opened, so registers made earlier keep working.
- A general comment is a row of `rc_comment` with the type `general` and no geometry. Earlier versions of the tool open such registers: they list general comments with an empty type, leave them off the map, and cannot edit their position.
- Images (png, jpg, gif, bmp, tif, webp) are copied into the attachments folder under new names, keeping the original name for display. Large images are shown reduced and are never changed.
- Every comment has a stable UUID, a per-set number, created and updated timestamps (UTC) and a version.
- Each save is one transaction: it either completes or changes nothing. If someone else saved the same comment since you opened it, you are asked before overwriting. A locked, read-only or damaged file gives a plain message instead of a partial save.

## Import and export

**Import** (CSV, TSV or Excel). The dialog guesses which file column fills each comment field, and you can change any of them. Each row needs text and a location:

- latitude and longitude, for a map location (decimal degrees, or degrees and minutes such as `55 30.5 N`);
- a KP, for a route point;
- KP and KP to, for a KP range.

KPs refer to the route you choose, which by default is the KP Mouse Tool's reference line. An **Image file(s)** column can name an image per row: a full path, or a file name looked up in the folder you choose (by default the file's own folder). Separate several images with `;`. The images are copied into the register with their comments, and **Show each comment's first image on the map** puts the first one in the map label. A missing or unsupported image is noted, and that row is imported without it. Rows are checked before import. Rows with no text, no location, a KP off the route, or an ID already in the register (or repeated in the file) are listed and skipped. With **Import rows without a location as general comments**, a row with no KP and no latitude/longitude becomes a general comment instead (a location that is given but cannot be read is still an error); this also brings back general comments from an export. Unknown statuses or priorities are kept and noted. An ID that is a comment UUID (from an earlier export) keeps that UUID; any other ID is stored as the comment's external ID. Dates are read day first (`03/04/2026` is 3 April).

**Export** (CSV or Excel) covers the visible rows (as filtered and sorted), the whole set or the whole register. It includes every comment field, the location and route reference (KP, KP convention, route start KP, RPL and revision), the reference condition, all custom fields, the counts of replies and images, and the reply text in one column. Image files are not included. Reply text is for reading only: re-importing the file adds the comments, not their replies. Excel files have an *About* sheet that states this.

### Importing a folder of pictures by KP

A spreadsheet containing only **Filename** and **KP** already works; no extra text or coordinate column is required:

1. Create a separate set, for example **Route images**, from the **Set** menu, and choose **Import**.
2. Open the Excel workbook (`.xlsx`) or CSV. Map **Comment** and **Image file(s)** to the **same Filename column**, and **KP** to the KP column. The filename becomes the comment text. Column mappings can share a source column even though automatic matching chooses each column only once.
3. Choose the route under **KPs refer to** and use **Image files from** to browse to the images subfolder of your QGIS project. Alternatively, filenames can include paths relative to the spreadsheet's folder, such as `images/crossing.png`.
4. Tick **Show each comment's first image on the map**, review the row notes for missing files or invalid KPs, and import.
5. Zoom to 1:50,000 or closer for the images to appear with the default label settings. Use the map menu's **Label contents…** to change the detail scale or image width. If the existing layers' labels cannot show images (older or custom labels), the import asks to update them, and a message offers to zoom in when the map is further out than the detail scale.

For pictures off the route, use **Filename**, **Latitude** and **Longitude** instead of KP. Map the filename to both **Comment** and **Image file(s)** as above, and map both coordinate columns (WGS84 latitude/longitude). The image stays at those coordinates. Choose a route under **KPs refer to** to calculate and save its **nearest KP** and **distance off route (metres)** automatically, using the shared route calculation also used by the **Nearest KP** algorithm; there is no need to run that algorithm first. Without a selected route, coordinate import still works, with KP and distance left empty. If a KP column is also present, leave it unmapped: for rows with both coordinates, the importer measures nearest KP from the position rather than using the supplied KP. This applies to ordinary comments as well as pictures.

Keep ordinary comments in their existing set and switch sets to review either collection. The dock shows **one set at a time**; it does not overlay both sets together. Imported images are copied into the register's attachments folder, so keep that folder beside the GeoPackage when sharing it.

Pictures use the existing scale-dependent QGIS labels and their collision handling. Crowded pictures may be hidden until you zoom in. Map labels draw the full-size image files from the attachments folder, so reducing the label width does not create smaller source files: for many large PNGs, import suitably reduced copies to limit image decoding and memory use. Performance depends on image dimensions and how many labels are in view; the tool does not guarantee every picture will be visible at once.

## Not in this version

KP schematic view, a browser interface, a formal audit trail, a revision transfer wizard, merge tools and agent integrations.
