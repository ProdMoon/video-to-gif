# Video to GIF Converter

동영상 파일을 GIF로 변환하는 데스크톱 앱입니다.
구간 선택, 크롭, FPS/스케일 조절을 지원하며, ffmpeg 2-pass 인코딩으로 고품질 GIF를 생성합니다.

![Python](https://img.shields.io/badge/Python-3.x-blue)

## 주요 기능

- 시작/끝 시간 슬라이더로 구간 선택
- 드래그로 크롭 영역 지정 (실시간 프리뷰)
- FPS (5~30), 스케일 (10~100%) 조절
- 2-pass 팔레트 기반 GIF 인코딩

## 요구사항

- Python 3
- ffmpeg (`brew install ffmpeg`)

## 설치 및 실행

```bash
bash setup.sh
python3 app.py
```

또는 수동 설치:

```bash
pip install -r requirements.txt
mkdir -p output
python3 app.py
```

## 사용법

1. **파일 열기** — 상단의 열기 버튼으로 동영상 파일 선택
2. **구간 설정** — 타임라인 슬라이더로 시작/끝 시간 조절
3. **크롭** — 프리뷰 화면에서 드래그하여 영역 지정 (선택사항)
4. **출력 설정** — FPS, 스케일, 저장 경로 조절
5. **변환** — Convert 버튼 클릭

생성된 GIF는 `output/` 폴더에 저장됩니다.

## 기술 스택

| 구분 | 사용 기술 |
|------|-----------|
| GUI | customtkinter |
| 영상 처리 | OpenCV, Pillow |
| GIF 인코딩 | ffmpeg (2-pass palettegen) |
