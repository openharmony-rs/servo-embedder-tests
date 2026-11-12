# SVG test pages

These pages are copied unchanged from [svg-tests-servo](https://github.com/mu-mostafa98/svg-tests-servo) by Mohamed
Mostafa, at commit [a71e7da](https://github.com/mu-mostafa98/svg-tests-servo/commit/a71e7da1a7e2bf2e8dfa993fcc894637c7fb0966).
They keep their MIT license, in [LICENSE](LICENSE), rather than the Apache-2.0 OR MIT of the rest of this project.

Each numbered page is a set of cards, a heading and an inline SVG each, about one part of SVG: basic shapes, paths,
strokes, gradients, patterns, markers, `viewBox`, transforms, `use` and `symbol`, text, clipping, masking and filters.
`servo-embedder-tests svg-cards` makes a page scenario of each card. The pages in `security/` probe the limits an SVG engine needs: on nesting, reference cycles and fetches from other
origins; `svg/test_security.py` loads them.

To update them, copy the files of a newer commit over these and name that commit above.
