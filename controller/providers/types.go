package providers

// AdGuard types

type AdGuardRewriteSpec struct {
	Domain string `json:"domain"`
	Answer string `json:"answer"`
}

type AdGuardRewriteObserved struct {
	Domain  string `json:"domain,omitempty"`
	Answer  string `json:"answer,omitempty"`
	Enabled bool   `json:"enabled,omitempty"`
}

type AdGuardRewriteStatus struct {
	Domain  string `json:"domain"`
	Answer  string `json:"answer"`
	Enabled bool   `json:"enabled"`
}

type AdGuardRewriteRecord struct {
	Domain  string `json:"domain"`
	Answer  string `json:"answer"`
	Enabled bool   `json:"enabled,omitempty"`
}

type AdGuardDeleteStatus struct {
	Domain  string `json:"domain"`
	Removed bool   `json:"removed"`
}
