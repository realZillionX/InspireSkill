"""Web-session protocols and the request machinery used by CLI and SDK.

Console Actions belong to inspire.platform.web.browser_api; 数据广场 has its own
host and envelope in inspire.platform.web.plaza. Session acquisition and renewal
belong to inspire.platform.web.session. Transport owns caller state, while its
blocking and native async drivers share request decisions. Workload business
rules and frontend presentation belong above this layer.
"""
