# video-translation-study-like-post-cast-

Trang web chạy trên máy (local) giúp **dịch video YouTube tiếng Trung / tiếng Anh sang tiếng Việt và lồng tiếng tự động**.

Dán link YouTube → hệ thống tải video, nghe và chép lời, dịch sang tiếng Việt, đọc lại bằng giọng Việt rồi ghép thành video mới (kèm phụ đề tiếng Việt).

## Cách hoạt động

| Bước | Công cụ | Chạy ở đâu |
|---|---|---|
| Tải video | [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Internet |
| Nhận dạng giọng nói | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (large-v3-turbo) | GPU trên máy |
| Dịch sang tiếng Việt | NLLB-200 1.3B của Meta (offline) hoặc Google Dịch | GPU trên máy / Internet |
| Lồng tiếng | Microsoft Edge TTS (giọng Hoài My / Nam Minh) | Internet |
| Ghép video | ffmpeg | Máy |

Tiếng gốc được giữ lại ở mức nhỏ làm nền (chỉnh được), giọng Việt được canh theo thời gian từng câu; câu nào dài hơn khoảng trống sẽ được đọc nhanh hơn (tối đa 1.5x).

## Yêu cầu

- Linux, Python 3.12 (script tự cài qua `uv`, không cần sudo)
- Khuyến nghị GPU NVIDIA ≥ 4GB VRAM (không có GPU vẫn chạy được trên CPU nhưng chậm hơn nhiều)
- Khoảng 5GB dung lượng trống cho các mô hình (tự tải ở lần chạy đầu)

## Cài đặt & chạy

```bash
git clone https://github.com/mochihat/video-translation-study-like-post-cast-.git
cd video-translation-study-like-post-cast-
./setup.sh      # chỉ chạy lần đầu
./run.sh        # khởi động
```

Mở trình duyệt: **http://localhost:5000**

## Tuỳ chọn trên giao diện

- **Ngôn ngữ gốc**: tự nhận diện / Trung / Anh
- **Giọng đọc**: nữ (Hoài My) hoặc nam (Nam Minh)
- **Chất lượng nhận dạng**: small (nhanh) / large-v3-turbo (mặc định) / large-v3 (tốt nhất)
- **Công cụ dịch**: offline (NLLB, mặc định) hoặc Google Dịch — nếu Google chặn sẽ tự chuyển sang offline
- **Âm lượng tiếng gốc**: 0–100%

Kết quả lưu trong thư mục `jobs/<mã>/`: `dubbed.mp4` (video lồng tiếng), `vi.srt`, `original.srt`.

## Lưu ý

Chỉ dùng cho mục đích học tập/cá nhân, tôn trọng bản quyền của người tạo video và điều khoản của YouTube.
