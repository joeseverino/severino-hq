// Package githubapi holds GitHub REST types generated from the vendored slice
// of GitHub's REST API description (api/vendor/github); edit the slice, never
// the generated file.
package githubapi

//go:generate go tool oapi-codegen -config ../../api/vendor/github/oapi-codegen.yaml ../../api/vendor/github/openapi.slice.json
