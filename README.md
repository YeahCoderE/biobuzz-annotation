# BioBuzz image annotation

This repository contains the public GitHub Pages site and the images it displays.
Files committed under `docs/images/` and annotation JSON files in `docs/data/`
are publicly readable by anyone.

## Open the site

After GitHub Pages is enabled for the repository, open:

`https://kan3d2.github.io/biobuzz-annotation/`

## Add images for everyone

1. Open the repository on GitHub and choose **Add file → Upload files**.
2. Upload image files into `docs/images/` and commit the change to `main`.
3. The Pages workflow regenerates the image list and publishes the update.

The **Upload images from this computer** button adds images only to the current
browser. To make those files available to other computers, upload them to
`docs/images/` in this repository.

## Share annotation drafts

The site saves drafts in the browser and can export `annotations_review.json`.
To share those drafts across computers, upload that exported file to
`docs/data/annotations_review.json` in the repository. The website reads the
shared file after Pages finishes deploying.

Draft labels are not training data. Do not train a model until the annotations
have been reviewed and explicitly approved.
