// Package cfapi holds Cloudflare API types generated from the vendored slice of
// Cloudflare's OpenAPI spec (api/vendor/cloudflare); edit the slice, never the
// generated file.
package cfapi

//go:generate go tool oapi-codegen -config ../../api/vendor/cloudflare/oapi-codegen.yaml ../../api/vendor/cloudflare/openapi.slice.json
