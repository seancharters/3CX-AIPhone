; A softphone (e.g. Zoiper, Linphone) can register as "tester" and dial 800 to reach the agent.
[tester]
type=endpoint
transport=transport-udp
context=from-tester
disallow=all
allow=g722,alaw,ulaw
auth=tester-auth
aors=tester
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes

[tester-auth]
type=auth
auth_type=userpass
username=tester
password=${TEST_SIP_PASSWORD}

[tester]
type=aor
max_contacts=1
