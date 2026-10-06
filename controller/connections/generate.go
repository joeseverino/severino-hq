package connections

// The document's types come from the schema HQ's registry emits
// (`manage.py bridge_contract`), so a setting's name exists once.
//go:generate go tool oapi-codegen -config ../api/connections-codegen.yaml ../api/hq-connections.openapi.json
