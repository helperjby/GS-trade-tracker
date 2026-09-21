"""SEAssist(거상 매크로, C:\\dev\\gersang) 패킷 코어의 벤더 사본.

`tools/sync_seassist_core.py` 만이 이 패키지의 파일을 바꾼다 — 출처 커밋·해시는 `VENDOR.json`.
예외는 `config.py`(이 프로젝트가 소유하는 심)와 이 파일뿐이다. 프로토콜 판정의 정본은 SEAssist
레포의 `gersang_protocol.py` 이고, 육의전 opcode·파서도 거기서 코퍼스 테스트와 함께 들어온 뒤
동기화로 가져온다(사본을 직접 고치지 않는다).
"""
