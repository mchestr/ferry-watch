{{/* Names, labels and the two config files, shared by the templates. */}}

{{- define "fw.labels" -}}
app.kubernetes.io/part-of: ferry-watch
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end }}

{{/* An image reference: repository[:tag][@digest]. */}}
{{- define "fw.image" -}}
{{- $ref := .repository -}}
{{- if .tag }}{{ $ref = printf "%s:%s" $ref .tag }}{{ end -}}
{{- if .digest }}{{ $ref = printf "%s@%s" $ref .digest }}{{ end -}}
{{- $ref -}}
{{- end }}

{{/* Browser images by digest, as ferry-control and the admission policy name them. */}}
{{- define "fw.browserImages" -}}
{{- $b := .Values.images.browser -}}
{{- if not $b.digests }}{{ fail "images.browser.digests needs the browser image's digest" }}{{ end -}}
{{- if gt (len $b.digests) 2 }}{{ fail "images.browser.digests holds at most two digests" }}{{ end -}}
{{- $out := list -}}
{{- range $b.digests -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" .) }}{{ fail (printf "not a sha256 digest: %s" .) }}{{ end -}}
{{- $out = append $out (printf "%s@%s" $b.repository .) -}}
{{- end -}}
{{- join "," $out -}}
{{- end }}

{{- define "fw.browserImage" -}}
{{- first (splitList "," (include "fw.browserImages" .)) -}}
{{- end }}

{{- define "fw.controlSA" -}}
system:serviceaccount:{{ .Release.Namespace }}:ferry-control
{{- end }}

{{- define "fw.hubUrl" -}}
ws://ferry-control.{{ .Release.Namespace }}.svc.cluster.local.:8766/agent/v1
{{- end }}

{{- define "fw.vncOrigin" -}}
https://{{ .Values.hosts.checkout }}
{{- end }}

{{- define "fw.pubkeys" -}}
{{- required "checkout.publicKeys is required (FW_VNC_PUBKEYS)" .Values.checkout.publicKeys -}}
{{- end }}

{{- define "fw.polling" -}}
{{- $p := .Values.polling -}}
{{- $out := dict "shortCheckInterval" $p.shortCheckInterval "horizonDays" $p.horizonDays "quietStart" $p.quietStart "quietEnd" $p.quietEnd -}}
{{- if $p.tiers }}{{ $_ := set $out "tiers" $p.tiers }}{{ end -}}
{{- toJson $out -}}
{{- end }}

{{/* ferry-control's config.json: the pod params here are the admission policy's params too. */}}
{{- define "fw.controlConfig" -}}
{{- $v := .Values -}}
{{- $pod := dict
  "image" (include "fw.browserImage" .)
  "hubUrl" (include "fw.hubUrl" .)
  "vncPubkeys" (include "fw.pubkeys" .)
  "vncOrigin" (include "fw.vncOrigin" .)
  "sandbox" $v.pod.sandbox
  "cpuRequest" $v.pod.cpuRequest
  "podMemory" $v.pod.memory
  "requireUserNamespace" $v.pod.requireUserNamespace
  "namespace" $v.browserNamespace -}}
{{- $config := dict
  "pod" $pod
  "fleet" (dict "maxBrowsersTotal" $v.fleet.maxBrowsersTotal "shortCheckSlots" $v.fleet.shortCheckSlots)
  "hold" $v.hold
  "polling" (include "fw.polling" . | fromJson)
  "limits" $v.limits
  "roles" $v.database.roles
  "checksPerMinute" $v.checksPerMinute
  "autoCart" $v.autoCart
  "alertOnlyFares" $v.alertOnlyFares
  "alertOnlyVehicles" $v.alertOnlyVehicles
  "listenPort" 8766
  "probePort" 9090 -}}
{{- toPrettyJson $config -}}
{{- end }}

{{/* ferry-web's config.json. Secrets are files, never here. */}}
{{- define "fw.webConfig" -}}
{{- $v := .Values -}}
{{- $o := $v.identity.oidc -}}
{{- $oidc := dict
  "issuer" (required "identity.oidc.issuer is required" $o.issuer)
  "clientId" (required "identity.oidc.clientId is required" $o.clientId)
  "redirectUri" (printf "https://%s/auth/callback" $v.hosts.dashboard)
  "scopes" $o.scopes
  "algorithms" $o.algorithms
  "usernameClaim" $o.usernameClaim
  "groupsClaim" $o.groupsClaim -}}
{{- $identity := dict
  "bootstrapAdmins" $v.identity.bootstrapAdmins
  "inviteClaim" $v.identity.inviteClaim
  "autoApproveGroups" $v.identity.autoApproveGroups
  "reverifyHours" $v.identity.reverifyHours -}}
{{- $config := dict
  "oidc" $oidc
  "checkout" (dict "origin" (include "fw.vncOrigin" .) "keyId" $v.checkout.keyId)
  "baseUrl" (printf "https://%s" $v.hosts.dashboard)
  "vapidSubject" $v.vapidSubject
  "identity" $identity
  "polling" (include "fw.polling" . | fromJson)
  "limits" $v.limits
  "kofiUrl" $v.kofiUrl
  "listenPort" 8765
  "staticDir" "/app/static" -}}
{{- if $v.smtp.enabled }}{{ $_ := set $config "smtp" (dict "host" $v.smtp.host "port" $v.smtp.port "sender" $v.smtp.sender) }}{{ end -}}
{{- toPrettyJson $config -}}
{{- end }}

{{/* The libpq variables for one login role. */}}
{{- define "fw.pgEnv" -}}
- {name: PGHOST, value: {{ .root.Values.database.host | quote }}}
- {name: PGPORT, value: {{ .root.Values.database.port | quote }}}
- {name: PGDATABASE, value: {{ .root.Values.database.name | quote }}}
- {name: PGSSLMODE, value: {{ .root.Values.database.sslmode | quote }}}
- {name: PGUSER, value: {{ .user | quote }}}
- name: PGPASSWORD
  valueFrom:
    secretKeyRef: {name: {{ .root.Values.secret.name }}, key: {{ .passwordKey }}}
{{- end }}

{{/* A container that runs as nobody, with nothing writable but /tmp. */}}
{{- define "fw.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities: {drop: [ALL]}
{{- end }}

{{- define "fw.podSecurity" -}}
runAsNonRoot: true
runAsUser: {{ . }}
runAsGroup: {{ . }}
seccompProfile: {type: RuntimeDefault}
{{- end }}

{{/* A memory quantity in Mi ("1536Mi", "12Gi"). */}}
{{- define "fw.mebibytes" -}}
{{- if hasSuffix "Gi" . }}{{ mul (trimSuffix "Gi" . | int) 1024 }}
{{- else if hasSuffix "Mi" . }}{{ trimSuffix "Mi" . | int }}
{{- else }}{{ fail (printf "use Mi or Gi: %s" .) }}{{ end -}}
{{- end }}

{{/* A CPU quantity in millicores ("200m", "1"). */}}
{{- define "fw.millicores" -}}
{{- if hasSuffix "m" . }}{{ trimSuffix "m" . | int }}{{ else }}{{ mul (. | int) 1000 }}{{ end -}}
{{- end }}

{{/* A NetworkPolicy peer: pods with these labels in that namespace. */}}
{{- define "fw.peer" -}}
- namespaceSelector:
    matchLabels: {kubernetes.io/metadata.name: {{ .namespace }}}
  podSelector:
    matchLabels:
      {{- toYaml .labels | nindent 6 }}
{{- end }}
{{/* Egress to kube-dns. */}}
{{- define "fw.dnsPeer" -}}
- to:
    - namespaceSelector:
        matchLabels: {kubernetes.io/metadata.name: kube-system}
      podSelector:
        matchLabels: {k8s-app: kube-dns}
  ports:
    - {protocol: UDP, port: 53}
    - {protocol: TCP, port: 53}
{{- end }}
