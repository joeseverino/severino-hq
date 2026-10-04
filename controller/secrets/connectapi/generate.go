// Package connectapi holds 1Password Connect API types generated from the
// vendored OpenAPI spec (api/vendor/onepassword-connect), filtered to the
// operations the renderer calls; edit the config, never the generated file.
package connectapi

//go:generate go tool oapi-codegen -config ../../api/vendor/onepassword-connect/oapi-codegen.yaml ../../api/vendor/onepassword-connect/openapi.yaml
