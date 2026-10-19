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
./run.sh        # khởi động (hoặc dùng install-service.sh bên dưới)
```

Mở trình duyệt: **http://localhost:5000**

### Cho web tự chạy khi bật máy (khuyên dùng)

```bash
./install-service.sh            # cài một lần, từ đó chỉ cần mở http://localhost:5000
./install-service.sh --remove   # gỡ
journalctl --user -u youtube-dub -f   # xem log
```

## Thư viện video

- Mọi video đã lồng tiếng được lưu trên máy (thư mục `jobs/`) và hiện ở trang chủ kèm ảnh bìa — mở lại xem bất cứ lúc nào, không cần mạng.
- Mỗi video có link riêng (`http://localhost:5000/#v=...`) để lưu bookmark.
- Web nhớ vị trí đang xem dở; bảng lời thoại song ngữ tự cuộn theo video.
- Dán lại link đã làm → mở bản đã lưu thay vì làm lại (muốn làm lại thì bấm **Làm lại**).
- Sau khi xong, file tạm được xoá — chỉ giữ video lồng tiếng, phụ đề và ảnh bìa.

## Nơi lưu video (ổ cứng ngoài)

Bấm **💾 Nơi lưu video** ở góc trên trang để chọn lưu video trên laptop hay trên một phân vùng / ổ cứng gắn ngoài (NTFS, exFAT, ext4…). Ổ được nhận theo UUID nên cắm vào cổng USB nào cũng được.

- Chỉ **video lồng tiếng và file tạm khi xử lý** nằm trên ổ đã chọn (`<ổ>/LongTiengVideos/`). Tên video, ảnh bìa, phụ đề, lời thoại song ngữ luôn ở trên laptop — rút ổ ra vẫn xem được thư viện và đọc lời thoại.
- Đổi nơi lưu có thể tick **Chuyển video đã có** — chép xong và kiểm tra rồi mới xoá bản cũ.
- Video trên ổ ngoài có nút **Giữ trên laptop** để chép một bản về máy, xem được khi không cắm ổ.
- Ổ chưa cắm / đầy (dưới 3GB) / không ghi được → hàng đợi tự tạm dừng và báo trên trang; cắm ổ vào là tự chạy tiếp, video tự mở lại.

**Rút ổ ra:** luôn bấm **⏏ Tháo ổ an toàn** ở đầu trang trước khi rút. Web dừng ghi vào ổ (video đang làm sẽ làm lại sau), tháo ổ rồi báo "có thể rút ổ ra". Rút thẳng khi đang ghi sẽ làm ổ NTFS bị đánh dấu lỗi ("dirty").

**Ổ NTFS không mở được** ("volume is dirty"): ổ chưa được tháo an toàn hoặc Windows bật Fast Startup. Cắm vào Windows, chạy `chkdsk X: /f` rồi tháo an toàn; hoặc trên Linux: `sudo ntfsfix -d /dev/sdX1`.
**Ổ ext4 báo không có quyền ghi**: `sudo chown $USER "/run/media/$USER/<tên ổ>"`.

## Xem trên điện thoại

Vào **💾 Nơi lưu video → 📱 Xem trên điện thoại → Bật**. Điện thoại dùng cùng Wi-Fi với laptop quét mã QR (hoặc gõ địa chỉ `http://<IP laptop>:5000` và nhập mã PIN 6 số) — mỗi điện thoại chỉ nhập PIN một lần.

- Laptop vào thẳng không cần PIN; máy khác bắt buộc PIN, chỉ nhận kết nối trong mạng nội bộ, sai 10 lần bị khoá 15 phút. **Đổi mã PIN** để đăng xuất mọi điện thoại.
- Wi-Fi trường học / công cộng thường chặn các thiết bị nói chuyện với nhau: khi đó bật điểm phát Wi-Fi trên điện thoại cho laptop kết nối vào.
- Laptop phải đang bật, không ngủ, và cắm ổ chứa video.

## Dịch cả playlist (treo máy)

1. Dán link playlist (`https://www.youtube.com/playlist?list=...` hoặc link video có `&list=...`), để tick **Dịch toàn bộ playlist**.
2. Bấm **Bắt đầu** rồi có thể đóng trình duyệt — các video được xử lý lần lượt.

Trong lúc chạy:
- Máy **không tự ngủ**, kể cả khi gập nắp laptop (nên cắm sạc). Xong hết thì máy ngủ lại bình thường.
- Nghỉ 20–40 giây giữa các video để YouTube không chặn.
- Bị YouTube chặn ("not a bot") hoặc mất mạng → tạm dừng và tự thử lại sau 5, 10, 20, 30 phút… (tối đa ~7 giờ), không bỏ video.
- Lỗi khác → tự thử lại 1 lần; video lỗi không làm dừng các video còn lại, có nút **Thử lại**.
- Tắt máy/khởi động lại giữa chừng → lần sau web tự làm tiếp các video còn dở.

Khi xem video trong playlist, xem xong một video sẽ tự chuyển sang video tiếp theo.

### Lỡ xoá video trong playlist?

Video bị xoá vẫn giữ chỗ trong playlist (hiện là **Đã xoá**). Để lấy lại, làm một trong các cách:
- bấm **Lồng tiếng lại** ngay ở ô video đó trên trang playlist;
- dán lại link video đó ở trang chủ — video sẽ tự về đúng vị trí cũ trong playlist;
- dán lại link playlist, hoặc bấm **Cập nhật từ YouTube** — thêm video mới, lồng tiếng lại video đã xoá và thử lại video lỗi (video đã xong được giữ nguyên, không tạo playlist trùng).

Trang playlist cũng có nút **Thử lại video lỗi** để chạy lại tất cả video lỗi một lần.

### Mất kết nối

Nếu máy chủ khởi động lại hoặc mạng chập chờn, trang web hiện thông báo và tự kết nối lại; video đang xem bị lỗi/đứng hình sẽ tự tải lại đúng chỗ đang xem.

### Khi YouTube báo "Sign in to confirm you're not a bot"

Đăng nhập YouTube trên Firefox, rồi ở ô **Đăng nhập YouTube qua** chọn **Firefox**. yt-dlp sẽ dùng phiên đăng nhập đó để tải.

## Tuỳ chọn trên giao diện

- **Ngôn ngữ gốc**: tự nhận diện / Trung / Anh
- **Giọng đọc**: nữ (Hoài My) hoặc nam (Nam Minh)
- **Chất lượng nhận dạng**: small (nhanh) / large-v3-turbo (mặc định) / large-v3 (tốt nhất)
- **Công cụ dịch**: offline (NLLB, mặc định) hoặc Google Dịch — nếu Google chặn sẽ tự chuyển sang offline
- **Âm lượng tiếng gốc**: 0–100%
- **Đăng nhập YouTube qua**: không / Firefox / Chrome (chỉ cần khi bị YouTube chặn)

Kết quả lưu trong thư mục `jobs/<mã>/`: `dubbed.mp4` (video lồng tiếng), `vi.srt`, `original.srt`.

## Lưu ý

Chỉ dùng cho mục đích học tập/cá nhân, tôn trọng bản quyền của người tạo video và điều khoản của YouTube.
