{{/* Labels of the objects this chart adds itself (the dependencies label their own). */}}
{{- define "observability.labels" -}}
app.kubernetes.io/part-of: chance-observability
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}
