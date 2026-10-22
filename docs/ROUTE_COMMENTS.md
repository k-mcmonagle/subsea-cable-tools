# Route Comments

Route Comments keeps spatial comments on cable routes and other project features: review remarks, survey observations, open questions. It is a register, not a workflow. There are no roles, approvals, accounts or locking, and no transitions are enforced between statuses.

## Adding a comment

With the **KP Mouse Tool** on, right-click the map and choose **Add Comment**:

- **At Nearest Route Point (KP …)** places the comment on the route at the KP shown.
- **At Cursor Location** keeps the position you clicked. Its nearest KP and distance off the route are recorded separately, so the actual position is never moved onto the route.
- **Over KP Range…** fixes the start at the nearest KP. Move along the route (the section is previewed) and click the end. **Esc** or a right-click cancels.

A short form opens with the author, today's date, the first status and the default priority already filled in, plus the location (KP, KP range, or latitude/longitude), which you can edit. Type the comment and press **Ctrl+Enter**. Tags and the set's custom fields are under **More fields**.

The KP Mouse Tool's other right-click entries are unchanged.

## The dock

**Plugins ▸ Subsea Cable Tools ▸ Route Comments** (also in the toolbar's *Experimental* dropdown). Drag its title bar off the QGIS window to use it as a separate window with minimise and maximise buttons; it reopens the way you left it.

- **Register button**: create a register, open one, recent registers, show the folder.
- **Set combo / Set menu**: switch comment sets. You can also create, rename or delete a set, edit its custom fields (text, number, date, yes/no, choice), and edit its status and priority choices. Statuses marked *counts as closed* are drawn grey and can be hidden.
- **Search** matches every word typed against the text, replies, number (`#12`), author, tags, route and custom values. **Filter** narrows by status, priority, reference condition and type.
- **Table** (across the top): click a header to sort. Right-click a header to show or hide columns or reset them. Column order, widths, visibility and sort are remembered. Double-click (or F2) the comment, status, priority, author, date, tags, external ID or a custom field to edit it in place; it is saved straight away, with the same check against changes made elsewhere as **Save**. Double-click a number or KP to zoom to it. Right-click rows to zoom to them, set their status or priority, copy their IDs or delete them.

Below the table is the selected comment. Drag the bar between them to share the height.

- **Header**: the comment's number and location, its **status** and **priority** (a change is saved at once), the up and down arrows (**Alt+Up** / **Alt+Down**) to step through the comments in the table's order, and **Follow on the map**, which pans the map to each comment you select here when it is out of view.
- **Thread**: the comment and its replies as a conversation, oldest first, with your own replies on the right. Type a reply and press **Send** (Ctrl+Enter). **Send & close** sends the reply (if you typed one) and sets the set's first closed status. On a closed comment the same button is **Send & reopen**. The thread shows each status change. Each message has a **⋯** button (or right-click it) to copy, edit or delete it. **Edit** opens the text in place: **Save** (Ctrl+Enter) or **Cancel** (Esc). An edited reply is marked *edited*. Editing a reply that changed the status keeps that change. Editing the comment itself works like editing its text in the table. If you move to another comment with an edit still open, you are asked whether to save it. Deleting the comment from the thread deletes it with its replies and images, as **Delete…** does. Deleting a reply that changed the status (*Send & close*) asks whether to put the status back. That is offered while the status is still the one the reply set and no later reply has changed it.
- **Details**: every other field of the comment. Edit it and **Save** (Ctrl+S).
- **Images**: the comment's images, beside the thread when the dock is wide and under it when it is narrow. The pane is small while the comment has no image. Zoom with the mouse wheel, drag to pan, and right-click to fit or show the image at actual size. Double-click opens it in your image viewer. When there are several images, the thumbnails and arrows switch between them. Add images with **Add…**, by dropping image files on the pane, or with **Paste** (**Ctrl+V** anywhere outside a text box), for example a screenshot. **Show on map** puts the image in the comment's map label (see below).

## On the map

The current set is drawn in a **Route comments** layer group, with points and map locations in one layer and KP ranges in another. Both layers read the register directly, filtered to the set: there is no copy of the comments to refill or clean up, and they are saved with the project like any other layer. Open comments are amber, closed ones grey, and comments whose route reference has changed or is unavailable are red. A KP range is a wide translucent band with a dark centre line, so it shows over a route line. Points that crowd together are clustered.

**Labels** grow as you zoom in, so the map stays readable at every scale:

- from 1:2 000 000, the comment number only (`#12`);
- from 1:250 000, the number and the start of the comment (24 characters);
- from 1:50 000, the details you choose (by default the number and the first 40 characters of the comment) and the comment's map image, in a small white tag.

**Label contents…** (map menu in the dock) picks the details: number, KP or KP range, status, priority, author, date, tags and the shortened comment, separated by spaces, middle dots or new lines. You can also write any QGIS expression (with the expression builder). The same dialog sets the scale of each step (or leaves a step out), the text size, the tag and the width of images in the tags; a step whose scale is not further out than the next one's is not shown, and the dialog says so. **Restore Defaults** puts them all back to the tool's defaults; labels set up before these steps existed keep their own two steps (number, then details) until you do. Where labels crowd, QGIS leaves some out, giving way to comments that need attention first, then open ones. **Labels** in the same menu turns them off.

**Images in labels.** A comment can show one of its images at the top of its detail tag, with the details underneath and the leader still pointing at the comment: tick **Show on map** under the image in the dock. It shows from the details step (1:50 000 by default), at the width set in **Label contents…** (30 mm by default, the height following the image). Each comment shows at most one image; untick it, or remove the image, to take it off the map. Labels made before images could be shown are updated when you first tick **Show on map**, after asking. The comment's text is always shown as plain text.

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

A route is looked up by layer ID, then by source, then by Workbench RPL, then by opening its file. A comment is **never** recomputed against a different or changed route. It keeps the KPs and position it was made with, and the condition says why they may no longer match. Conditions are re-checked when the register reloads; use **Check route references now** after editing a route. Moving comments to a new revision is not part of this version.

## Storage

A register is a GeoPackage you choose (by default `<project>_comments.gpkg` beside the project), with images copied into `<name>_attachments/` beside it. Keep the two together when moving or sharing a register. The project remembers its register and set, and recent registers are listed in the register menu. Several projects, or several QGIS sessions, can open the same register. The register uses SQLite's standard (rollback) journal rather than a write-ahead log, so any program, QGIS included, can read it without the side files a write-ahead log needs. The dock picks up changes saved elsewhere every few seconds.

- Tables: `rc_set`, `rc_field`, `rc_comment` (a GeoPackage feature layer that QGIS can open directly), `rc_reply`, `rc_attachment`, `rc_meta`. Newer versions only add columns, such as the image shown on the map and the status a reply set. They are added when the register is opened, so registers made earlier keep working.
- Images (png, jpg, gif, bmp, tif, webp) are copied into the attachments folder under new names, keeping the original name for display. Large images are shown reduced and are never changed.
- Every comment has a stable UUID, a per-set number, created and updated timestamps (UTC) and a version.
- Each save is one transaction: it either completes or changes nothing. If someone else saved the same comment since you opened it, you are asked before overwriting. A locked, read-only or damaged file gives a plain message instead of a partial save.

## Import and export

**Import** (CSV, TSV or Excel). The dialog guesses which file column fills each comment field, and you can change any of them. Each row needs text and a location:

- latitude and longitude, for a map location (decimal degrees, or degrees and minutes such as `55 30.5 N`);
- a KP, for a route point;
- KP and KP to, for a KP range.

KPs refer to the route you choose, which by default is the KP Mouse Tool's reference line. An **Image file(s)** column can name an image per row: a full path, or a file name looked up in the folder you choose (by default the file's own folder). Separate several images with `;`. The images are copied into the register with their comments, and **Show each comment's first image on the map** puts the first one in the map label. A missing or unsupported image is noted, and that row is imported without it. Rows are checked before import. Rows with no text, no location, a KP off the route, or an ID already in the register (or repeated in the file) are listed and skipped. Unknown statuses or priorities are kept and noted. An ID that is a comment UUID (from an earlier export) keeps that UUID; any other ID is stored as the comment's external ID. Dates are read day first (`03/04/2026` is 3 April).

**Export** (CSV or Excel) covers the visible rows (as filtered and sorted), the whole set or the whole register. It includes every comment field, the location and route reference (KP, KP convention, route start KP, RPL and revision), the reference condition, all custom fields, the counts of replies and images, and the reply text in one column. Image files are not included. Reply text is for reading only: re-importing the file adds the comments, not their replies. Excel files have an *About* sheet that states this.

## Not in this version

KP schematic view, a browser interface, a formal audit trail, a revision transfer wizard, merge tools and agent integrations.
