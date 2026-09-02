<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE TS>
<TS version="2.1" language="en_US">
<context>
    <name>AppButton</name>
    <message>
        <location filename="../qml/components/AppButton.qml" line="153"/>
        <source>Đang xử lý…</source>
        <translation>Processing…</translation>
    </message>
</context>
<context>
    <name>AppController</name>
    <message>
        <location filename="../controller.py" line="530"/>
        <location filename="../controller.py" line="590"/>
        <location filename="../controller.py" line="619"/>
        <source>Yêu cầu không hợp lệ: {}</source>
        <translation>Invalid request: {}</translation>
    </message>
    <message>
        <location filename="../controller.py" line="567"/>
        <source>Bản văn quá dài ({chars:,} ký tự, giới hạn {limit:,}). Hãy dùng tab Sách nói (EPUB) để tạo văn bản dài theo từng chương.</source>
        <translation>Text is too long ({chars:,} characters, limit {limit:,}). Use the Audiobook (EPUB) tab to synthesize long text chapter by chapter.</translation>
    </message>
    <message>
        <location filename="../controller.py" line="664"/>
        <source>Chưa có gì để xuất — hãy tổng hợp âm thanh trước.</source>
        <translation>Nothing to export yet — generate audio first.</translation>
    </message>
    <message>
        <location filename="../controller.py" line="667"/>
        <source>Đang xuất một tệp khác — vui lòng đợi.</source>
        <translation>Another export is in progress — please wait.</translation>
    </message>
    <message>
        <location filename="../controller.py" line="677"/>
        <source>Xuất WAV thất bại: {}</source>
        <translation>WAV export failed: {}</translation>
    </message>
    <message>
        <location filename="../controller.py" line="727"/>
        <source>Chưa có gì để phát — hãy tổng hợp âm thanh trước.</source>
        <translation>Nothing to play yet — generate audio first.</translation>
    </message>
    <message>
        <location filename="../controller.py" line="805"/>
        <location filename="../controller.py" line="808"/>
        <location filename="../controller.py" line="821"/>
        <location filename="../controller.py" line="977"/>
        <location filename="../controller.py" line="986"/>
        <source>Hệ thống này không phát được âm thanh.</source>
        <translation>Audio playback is unavailable on this system.</translation>
    </message>
    <message>
        <location filename="../controller.py" line="1056"/>
        <source>Đang nhập một tệp khác — vui lòng đợi.</source>
        <translation>Another import is in progress — please wait.</translation>
    </message>
    <message>
        <location filename="../controller.py" line="1065"/>
        <source>Không tìm thấy tệp: {}</source>
        <translation>File not found: {}</translation>
    </message>
    <message>
        <location filename="../controller.py" line="1069"/>
        <source>Lỗi nhập tệp: {}</source>
        <translation>Could not import file: {}</translation>
    </message>
    <message>
        <location filename="../controller.py" line="1365"/>
        <source>modelRepo phải là chuỗi ký tự.</source>
        <translation>modelRepo must be a string.</translation>
    </message>
    <message>
        <location filename="../controller.py" line="1378"/>
        <source>defaultVoice phải là chuỗi ký tự không trống.</source>
        <translation>defaultVoice must be a non-empty string.</translation>
    </message>
    <message>
        <location filename="../controller.py" line="1389"/>
        <source>outputDir phải là chuỗi ký tự.</source>
        <translation>outputDir must be a string.</translation>
    </message>
    <message>
        <location filename="../controller.py" line="1404"/>
        <source>temperature phải là số trong khoảng 0.05 đến 2.0.</source>
        <translation>temperature must be a number between 0.05 and 2.0.</translation>
    </message>
</context>
<context>
    <name>AudiobookController</name>
    <message>
        <location filename="../audiobook_controller.py" line="96"/>
        <source>Chương {title} quá dài ({chars:,} ký tự, giới hạn {limit:,}). </source>
        <translation>Chapter {title} is too long ({chars:,} characters, limit {limit:,}). </translation>
    </message>
    <message>
        <location filename="../audiobook_controller.py" line="98"/>
        <source>Hãy dùng bản EPUB có chương ngắn hơn.</source>
        <translation>Please use an EPUB with shorter chapters.</translation>
    </message>
    <message>
        <location filename="../audiobook_controller.py" line="511"/>
        <source>Đang mở một sách khác — vui lòng đợi.</source>
        <translation>Another book is opening — please wait.</translation>
    </message>
    <message>
        <location filename="../audiobook_controller.py" line="515"/>
        <location filename="../audiobook_controller.py" line="533"/>
        <source>Không tìm thấy tệp: {}</source>
        <translation>File not found: {}</translation>
    </message>
    <message>
        <location filename="../audiobook_controller.py" line="519"/>
        <source>Không hỗ trợ loại tệp &apos;{}&apos;. Sách nói phải là tệp .epub.</source>
        <translation>Unsupported file type &apos;{}&apos;. Audiobooks must be .epub files.</translation>
    </message>
    <message>
        <location filename="../audiobook_controller.py" line="1147"/>
        <location filename="../audiobook_controller.py" line="1158"/>
        <source>Chưa mở sách nào.</source>
        <translation>No book is open.</translation>
    </message>
</context>
<context>
    <name>AudiobookTab</name>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="88"/>
        <source>Sẵn sàng</source>
        <translation>Ready</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="89"/>
        <source>Đang tạo…</source>
        <translation>Rendering…</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="90"/>
        <source>Lỗi</source>
        <translation>Failed</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="91"/>
        <source>Chờ</source>
        <translation>Pending</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="157"/>
        <source>Chọn sách EPUB</source>
        <translation>Choose an EPUB book</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="220"/>
        <source>Sách nói</source>
        <translation>Audiobooks</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="221"/>
        <source>Nhập sách EPUB, tạo âm thanh từng chương một lần và nghe liền mạch — ứng dụng ghi nhớ vị trí bạn đang nghe.</source>
        <translation>Import an EPUB, render each chapter&apos;s audio once, and listen seamlessly — the app remembers where you left off.</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="229"/>
        <source>Thư viện</source>
        <translation>Library</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="231"/>
        <source>%1 sách</source>
        <translation>%1 books</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="232"/>
        <source>Kéo thả tệp .epub vào đây, hoặc bấm “Thêm EPUB…”</source>
        <translation>Drag a .epub file here, or click “Add EPUB…”</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="241"/>
        <source>Thêm EPUB…</source>
        <translation>Add EPUB…</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="273"/>
        <source>Chưa có sách nào. Thêm một tệp .epub để bắt đầu.</source>
        <translation>No books yet. Add a .epub file to get started.</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="343"/>
        <location filename="../qml/AudiobookTab.qml" line="378"/>
        <source>%1 chương</source>
        <translation>%1 chapters</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="355"/>
        <location filename="../qml/AudiobookTab.qml" line="358"/>
        <source>Xóa sách khỏi thư viện</source>
        <translation>Remove book from library</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="387"/>
        <location filename="../qml/AudiobookTab.qml" line="390"/>
        <source>Tự chuyển chương</source>
        <translation>Auto-advance chapters</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="400"/>
        <source>Xuất WAV</source>
        <translation>Export WAV</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="412"/>
        <source>Tạo tất cả</source>
        <translation>Render all</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="422"/>
        <source>Chọn thư mục xuất các chương</source>
        <translation>Choose the chapter export folder</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="438"/>
        <source>Giọng đọc:</source>
        <translation>Voice:</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="487"/>
        <source>%1/%2 đã xong</source>
        <translation>%1/%2 done</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="543"/>
        <source>còn ~%1</source>
        <translation>~%1 left</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="574"/>
        <source>Tổng: %1/%2 chương</source>
        <translation>Overall: %1/%2 chapters</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="697"/>
        <source>%1 ký tự</source>
        <translation>%1 characters</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="722"/>
        <location filename="../qml/AudiobookTab.qml" line="728"/>
        <source>Tạo âm thanh cho chương này</source>
        <translation>Render audio for this chapter</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="474"/>
        <source>Đang tạo chương %1…</source>
        <translation>Rendering chapter %1…</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="556"/>
        <source>Hủy</source>
        <translation>Cancel</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="742"/>
        <location filename="../qml/AudiobookTab.qml" line="745"/>
        <source>Dừng tạo âm thanh</source>
        <translation>Stop rendering</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="935"/>
        <source>Sao chép chương</source>
        <translation>Copy chapter</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="938"/>
        <source>Sao chép toàn bộ văn bản chương</source>
        <translation>Copy the full chapter text</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="949"/>
        <location filename="../qml/AudiobookTab.qml" line="951"/>
        <source>Đóng vùng đọc văn bản</source>
        <translation>Close the reading pane</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="1163"/>
        <source>Chọn một chương để bắt đầu</source>
        <translation>Select a chapter to start</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="1196"/>
        <source>Văn bản</source>
        <translation>Transcript</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="1199"/>
        <location filename="../qml/AudiobookTab.qml" line="1200"/>
        <source>Xem văn bản chương khi nghe</source>
        <translation>Show the chapter text while listening</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="1214"/>
        <location filename="../qml/AudiobookTab.qml" line="1217"/>
        <source>Chương trước</source>
        <translation>Previous chapter</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="1229"/>
        <source>Tạm dừng</source>
        <translation>Pause</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="1229"/>
        <source>Phát</source>
        <translation>Play</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="1247"/>
        <location filename="../qml/AudiobookTab.qml" line="1251"/>
        <source>Chương tiếp theo</source>
        <translation>Next chapter</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="1277"/>
        <source>Vị trí phát</source>
        <translation>Playback position</translation>
    </message>
    <message>
        <location filename="../qml/AudiobookTab.qml" line="835"/>
        <source>Không thể xử lý sách nói</source>
        <translation>Could not process audiobook</translation>
    </message>
</context>
<context>
    <name>CloningTab</name>
    <message>
        <location filename="../qml/CloningTab.qml" line="78"/>
        <source>Chọn tệp âm thanh tham chiếu</source>
        <translation>Choose a reference audio file</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="90"/>
        <source>Xóa giọng nói?</source>
        <translation>Delete voice?</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="109"/>
        <source>Giọng nói này sẽ bị xóa khỏi danh mục đã sao chép.</source>
        <translation>This voice will be removed from the cloned voice catalog.</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="124"/>
        <source>Giữ lại</source>
        <translation>Keep voice</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="131"/>
        <source>Xóa giọng</source>
        <translation>Delete voice</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="149"/>
        <source>Sao chép giọng nói</source>
        <translation>Voice Cloning</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="150"/>
        <source>Tạo giọng đọc tùy chỉnh từ một đoạn âm thanh mẫu 3–8 giây, 100% riêng tư trên thiết bị.</source>
        <translation>Create a custom voice from a 3–8 second sample clip, 100% private on-device.</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="167"/>
        <source>Cam kết bản quyền &amp; Trách nhiệm sử dụng</source>
        <translation>Copyright &amp; Usage Responsibility</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="168"/>
        <source>Riêng tư trên thiết bị</source>
        <translation>Private on-device</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="186"/>
        <source>Bạn xác nhận có quyền sử dụng giọng nói trong tệp tham chiếu này và đã có sự đồng ý của chính người được sao chép đối với việc tạo bản sao giọng nói. Bản sao được lưu trên máy của bạn; việc bảo quản và sử dụng bản sao giọng nói là trách nhiệm của bạn, và không được dùng để mạo danh hoặc gây nhầm lẫn cho người khác.</source>
        <translation>You confirm you have the right to use the voice in this reference file and the consent of the person being cloned to create this voice copy. The copy is stored on your machine; keeping and using it responsibly is up to you, and it must not be used to impersonate or mislead others.</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="212"/>
        <source>Tôi đồng ý</source>
        <translation>I agree</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="232"/>
        <source>Tệp âm thanh tham chiếu</source>
        <translation>Reference audio file</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="233"/>
        <source>Kéo thả tệp vào khung, hoặc chọn từ máy — đoạn giọng đọc rõ ràng, ít nhiễu</source>
        <translation>Drag a file into the box, or browse — a clear, low-noise speech clip works best</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="293"/>
        <source>Chưa chọn tệp</source>
        <translation>No file selected</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="303"/>
        <source>Chọn đoạn âm 3–8 giây, chỉ có tiếng nói, ít nhiễu.</source>
        <translation>Pick a 3–8 second clip with speech only and little noise.</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="314"/>
        <source>Chọn tệp…</source>
        <translation>Browse…</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="329"/>
        <location filename="../qml/CloningTab.qml" line="331"/>
        <source>Khử nhiễu trước khi sao chép</source>
        <translation>Denoise before cloning</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="341"/>
        <source>Nghe bản khử nhiễu</source>
        <translation>Listen to the denoised version</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="351"/>
        <source>Phát thử</source>
        <translation>Preview</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="364"/>
        <source>Đặt tên và tạo giọng</source>
        <translation>Name and create the voice</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="365"/>
        <source>Giọng sau khi tạo sẽ hiển thị trong danh mục lựa chọn giọng đọc</source>
        <translation>The new voice appears in the voice picker once created</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="380"/>
        <source>Tên giọng mới (vd: Giọng đọc truyện)</source>
        <translation>New voice name (e.g. Storyteller Voice)</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="407"/>
        <source>Tạo giọng nói</source>
        <translation>Create voice</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="419"/>
        <source>Giọng đã sao chép</source>
        <translation>Cloned voices</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="420"/>
        <source>Danh sách các giọng đọc tùy chỉnh đang lưu trên máy</source>
        <translation>Custom voices currently saved on this machine</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="491"/>
        <source>Sẵn sàng dùng trong mọi studio</source>
        <translation>Ready to use in every studio</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="503"/>
        <source>Xóa</source>
        <translation>Delete</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="540"/>
        <source>Chưa có giọng sao chép nào</source>
        <translation>No cloned voices yet</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="549"/>
        <source>Giọng bạn tạo ở trên sẽ xuất hiện tại đây</source>
        <translation>Voices you create above will appear here</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="566"/>
        <source>Đang xử lý…</source>
        <translation>Processing…</translation>
    </message>
    <message>
        <location filename="../qml/CloningTab.qml" line="604"/>
        <source>Không thể tạo giọng nói</source>
        <translation>Could not create voice</translation>
    </message>
</context>
<context>
    <name>Main</name>
    <message>
        <location filename="../qml/Main.qml" line="45"/>
        <source>VieNeuTTS — On-Device AI Audio Workstation</source>
        <translation>VieNeuTTS — On-Device AI Audio Workstation</translation>
    </message>
    <message>
        <location filename="../qml/Main.qml" line="133"/>
        <source>VieNeuTTS</source>
        <translation>VieNeuTTS</translation>
    </message>
    <message>
        <location filename="../qml/Main.qml" line="158"/>
        <source>AI Audio Workstation</source>
        <translation>AI Audio Workstation</translation>
    </message>
    <message>
        <location filename="../qml/Main.qml" line="168"/>
        <source>Chức năng</source>
        <translation>Features</translation>
    </message>
    <message>
        <location filename="../qml/Main.qml" line="266"/>
        <source>Phần cứng &amp; Engine</source>
        <translation>Hardware &amp; Engine</translation>
    </message>
    <message>
        <location filename="../qml/Main.qml" line="278"/>
        <source>Sẵn sàng</source>
        <translation>Ready</translation>
    </message>
    <message>
        <location filename="../qml/Main.qml" line="378"/>
        <source>Không phát hiện thiết bị âm thanh — chế độ chỉ xuất tệp (export-only).</source>
        <translation>No audio device detected — export-only mode.</translation>
    </message>
    <message>
        <location filename="../qml/Main.qml" line="389"/>
        <source>Kiểm tra lại</source>
        <translation>Check again</translation>
    </message>
    <message>
        <location filename="../qml/Main.qml" line="445"/>
        <source>Thiếu dữ liệu mô hình</source>
        <translation>Missing model data</translation>
    </message>
    <message>
        <location filename="../qml/Main.qml" line="456"/>
        <source>Các tệp trọng lượng mô hình (model weights) chưa có trên máy, nên không thể tổng hợp giọng nói. Hãy tải gói ngoại tuyến một lần duy nhất bằng lệnh sau, chạy từ thư mục gốc của dự án:</source>
        <translation>The model weights are not on this machine, so speech cannot be synthesized. Download the offline bundle once with the command below, run from the project root:</translation>
    </message>
    <message>
        <location filename="../qml/Main.qml" line="476"/>
        <source>python scripts/fetch_models.py</source>
        <translation>python scripts/fetch_models.py</translation>
    </message>
    <message>
        <location filename="../qml/Main.qml" line="486"/>
        <source>Sau khi tải xong, nhấn “Thử lại” và thử tạo lại âm thanh.</source>
        <translation>Once the download finishes, press “Retry” and generate audio again.</translation>
    </message>
    <message>
        <location filename="../qml/Main.qml" line="500"/>
        <source>Thử lại</source>
        <translation>Retry</translation>
    </message>
</context>
<context>
    <name>ParagraphTab</name>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="50"/>
        <location filename="../qml/ParagraphTab.qml" line="56"/>
        <location filename="../qml/ParagraphTab.qml" line="70"/>
        <source>Không thể nhập tệp</source>
        <translation>Could not import file</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="118"/>
        <source>Chọn tệp văn bản</source>
        <translation>Choose a text file</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="148"/>
        <source>Đoạn văn / Tệp</source>
        <translation>Paragraphs / Files</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="149"/>
        <source>Dán văn bản dài hoặc nhập tệp tài liệu. Hệ thống tự động phân đoạn thông minh và truyền phát âm thanh tức thì.</source>
        <translation>Paste long text or import a document. Smart auto-segmentation streams the audio instantly.</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="156"/>
        <source>Nội dung tài liệu</source>
        <translation>Document content</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="157"/>
        <source>Kéo thả tệp vào đây, hoặc dán văn bản trực tiếp</source>
        <translation>Drag a file here, or paste text directly</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="168"/>
        <source>Nhập tệp…</source>
        <translation>Import file…</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="190"/>
        <source>%1 ký tự</source>
        <translation>%1 characters</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="205"/>
        <source>%1 từ (~%2 phút)</source>
        <translation>%1 words (~%2 min)</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="217"/>
        <source>Xóa</source>
        <translation>Clear</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="235"/>
        <source>Hỗ trợ:</source>
        <translation>Supported:</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="279"/>
        <source>Dán văn bản dài / nhiều đoạn văn vào đây, hoặc kéo thả tệp tài liệu vào khung này…</source>
        <translation>Paste long or multi-paragraph text here, or drag a document file into this box…</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="309"/>
        <source>Giọng đọc &amp; Tổng hợp</source>
        <translation>Voice &amp; Synthesis</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="321"/>
        <source>Giọng đọc:</source>
        <translation>Voice:</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="345"/>
        <source>Tạo âm thanh</source>
        <translation>Generate audio</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="349"/>
        <location filename="../qml/ParagraphTab.qml" line="402"/>
        <source>Nhập văn bản để tạo âm thanh.</source>
        <translation>Enter text to generate audio.</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="350"/>
        <source>Tổng hợp phát trực tiếp (Ctrl+Return)</source>
        <translation>Synthesize and stream (Ctrl+Return)</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="361"/>
        <source>Phát</source>
        <translation>Play</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="361"/>
        <source>Dừng</source>
        <translation>Stop</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="366"/>
        <source>Tạo âm thanh trước khi phát.</source>
        <translation>Generate audio before playing.</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="369"/>
        <source>Dừng phát lại</source>
        <translation>Stop replay</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="370"/>
        <source>Phát lại âm thanh vừa tạo</source>
        <translation>Replay the generated audio</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="367"/>
        <source>Không phát hiện thiết bị âm thanh.</source>
        <translation>No audio device detected.</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="387"/>
        <source>Xuất WAV</source>
        <translation>Export WAV</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="390"/>
        <source>Tạo âm thanh trước khi xuất WAV.</source>
        <translation>Generate audio before exporting WAV.</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="404"/>
        <source>Tạo âm thanh trước khi phát hoặc xuất.</source>
        <translation>Generate audio before playing or exporting.</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="445"/>
        <source>Đang tổng hợp…</source>
        <translation>Synthesizing…</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="504"/>
        <source>Hủy</source>
        <translation>Cancel</translation>
    </message>
    <message>
        <location filename="../qml/ParagraphTab.qml" line="518"/>
        <source>Cần chú ý</source>
        <translation>Attention needed</translation>
    </message>
</context>
<context>
    <name>PlaybackController</name>
    <message>
        <location filename="../playback.py" line="252"/>
        <source>Hệ thống này không phát được âm thanh.</source>
        <translation>Audio playback is unavailable on this system.</translation>
    </message>
</context>
<context>
    <name>SettingsTab</name>
    <message>
        <location filename="../qml/SettingsTab.qml" line="37"/>
        <source>Tự động (ONNX/CPU hoặc CUDA)</source>
        <translation>Auto (ONNX/CPU or CUDA)</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="38"/>
        <source>ONNX Runtime (CPU)</source>
        <translation>ONNX Runtime (CPU)</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="39"/>
        <source>PyTorch (NVIDIA CUDA)</source>
        <translation>PyTorch (NVIDIA CUDA)</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="45"/>
        <source>int8 — nhanh (mặc định)</source>
        <translation>int8 — fast (default)</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="46"/>
        <source>fp32 — chất lượng tối đa</source>
        <translation>fp32 — maximum quality</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="50"/>
        <location filename="../qml/SettingsTab.qml" line="59"/>
        <source>Theo hệ điều hành</source>
        <translation>Match operating system</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="51"/>
        <source>Giao diện Sáng</source>
        <translation>Light theme</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="52"/>
        <source>Giao diện Tối</source>
        <translation>Dark theme</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="114"/>
        <source>Chọn thư mục xuất âm thanh</source>
        <translation>Choose the audio export folder</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="126"/>
        <source>Cài đặt hệ thống</source>
        <translation>System settings</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="127"/>
        <source>Cấu hình engine suy luận, âm thanh, giọng mặc định và giao diện hiển thị.</source>
        <translation>Configure the inference engine, audio, default voice, and appearance.</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="133"/>
        <source>Engine &amp; Phần cứng</source>
        <translation>Engine &amp; Hardware</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="134"/>
        <source>Thiết lập môi trường tính toán AI cho VieNeu-TTS v3 Turbo</source>
        <translation>Set up the AI compute environment for VieNeu-TTS v3 Turbo</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="238"/>
        <location filename="../qml/SettingsTab.qml" line="267"/>
        <source>Backend suy luận</source>
        <translation>Inference backend</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="247"/>
        <source>Chọn ONNX Runtime (CPU) hoặc PyTorch (NVIDIA GPU)</source>
        <translation>Choose ONNX Runtime (CPU) or PyTorch (NVIDIA GPU)</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="317"/>
        <source>Độ chính xác mô hình (ONNX)</source>
        <translation>Model precision (ONNX)</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="326"/>
        <source>int8: tối ưu tốc độ &amp; bộ nhớ; fp32: chất lượng cao nhất</source>
        <translation>int8: optimized speed &amp; memory; fp32: highest quality</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="344"/>
        <source>Độ chính xác mô hình</source>
        <translation>Model precision</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="397"/>
        <source>Nguồn mô hình (Hugging Face)</source>
        <translation>Model source (Hugging Face)</translation>
    </message>
    <message>
        <source>Để trống dùng repo chính thức; dán dạng &apos;người_dùng/tên_repo&apos; để dùng phiên bản khác</source>
        <translation type="vanished">Leave empty to use the official repo; paste as &apos;user/repo_name&apos; to use another version</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="406"/>
        <source>Tùy chỉnh</source>
        <translation>Custom</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="407"/>
        <source>Chính thức</source>
        <translation>Official</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="418"/>
        <source>Sử dụng mô hình gốc chính thức hoặc chỉ định repository tùy chỉnh từ Hugging Face</source>
        <translation>Use the official backbone model or specify a custom Hugging Face repository</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="438"/>
        <source>pnnbao-ump/VieNeu-TTS-v3-Turbo (Mặc định)</source>
        <translation>pnnbao-ump/VieNeu-TTS-v3-Turbo (Default)</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="439"/>
        <source>Chọn mô hình chính thức mặc định</source>
        <translation>Select official default model</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="450"/>
        <source>Repo tùy chỉnh</source>
        <translation>Custom repo</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="451"/>
        <source>Nhập repository tùy chỉnh</source>
        <translation>Enter custom repository</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="513"/>
        <source>Nguồn mô hình</source>
        <translation>Model source</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="530"/>
        <location filename="../qml/SettingsTab.qml" line="531"/>
        <source>Khôi phục repo chính thức mặc định</source>
        <translation>Reset to official default repository</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="547"/>
        <source>Hugging Face</source>
        <translation>Hugging Face</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="548"/>
        <location filename="../qml/SettingsTab.qml" line="549"/>
        <source>Mở trang mô hình trên Hugging Face</source>
        <translation>Open model repository on Hugging Face</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="580"/>
        <source>Mô hình mặc định chính thức (48kHz, hỗ trợ tiếng Việt và tiếng Anh). Tự động lưu cache tại ~/.cache/huggingface/hub/</source>
        <translation>Official default model (48kHz, Vietnamese and English code-switching). Cached automatically at ~/.cache/huggingface/hub/</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="582"/>
        <source>Repository hợp lệ: huggingface.co/%1 (sẽ tự động tải khi khởi động engine)</source>
        <translation>Valid repository: huggingface.co/%1 (downloaded automatically on engine start)</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="584"/>
        <source>Định dạng chưa đúng: cần có dạng &apos;tác_giả/tên_repo&apos; (ví dụ: username/custom-model, không có khoảng trắng)</source>
        <translation>Invalid format: expected &apos;owner/repo_name&apos; (e.g. username/custom-model, without whitespace)</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="600"/>
        <source>Áp dụng khi khởi động lại</source>
        <translation>Applies after restart</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="601"/>
        <source>Thay đổi backend/độ chính xác/nguồn mô hình sẽ áp dụng ở lần khởi động engine tiếp theo.</source>
        <translation>Backend/precision/model-source changes apply the next time the engine starts.</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="611"/>
        <source>Tổng hợp &amp; Âm thanh</source>
        <translation>Synthesis &amp; Audio</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="612"/>
        <source>Thiết lập thông số giọng đọc và thư mục lưu trữ</source>
        <translation>Configure voice parameters and the storage folder</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="649"/>
        <source>Giọng đọc mặc định</source>
        <translation>Default voice</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="659"/>
        <source>Giọng được tự động chọn khi mở ứng dụng</source>
        <translation>The voice pre-selected when the app opens</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="717"/>
        <source>Thư mục xuất âm thanh</source>
        <translation>Audio export folder</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="725"/>
        <source>Vị trí lưu trữ các tệp âm thanh xuất ra (.wav)</source>
        <translation>Where exported audio files (.wav) are saved</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="771"/>
        <source>Mặc định: ~/Music/VieNeuTTS</source>
        <translation>Default: ~/Music/VieNeuTTS</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="783"/>
        <source>Thay đổi…</source>
        <translation>Change…</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="792"/>
        <location filename="../qml/SettingsTab.qml" line="793"/>
        <source>Khôi phục thư mục mặc định</source>
        <translation>Restore default folder</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="839"/>
        <source>Temperature (Độ biến thiên)</source>
        <translation>Temperature (variation)</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="849"/>
        <source>0.6 – 0.8: Chuẩn, ổn định tự nhiên; 0.9+: Nhiều biểu cảm và ngữ điệu hơn</source>
        <translation>0.6 – 0.8: standard, naturally stable; 0.9+: more expressive, more intonation</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="867"/>
        <source>Temperature</source>
        <translation>Temperature</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="888"/>
        <source>Giao diện &amp; Trải nghiệm</source>
        <translation>Appearance &amp; Experience</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="889"/>
        <source>Tùy chỉnh chế độ hiển thị màu sắc và phong cách giao diện</source>
        <translation>Customize the color mode and interface style</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="925"/>
        <location filename="../qml/SettingsTab.qml" line="952"/>
        <source>Chế độ màu sắc</source>
        <translation>Color mode</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="935"/>
        <source>Chọn giao diện Tối, Sáng hoặc theo hệ thống — áp dụng ngay lập tức</source>
        <translation>Choose Dark, Light, or system appearance — applies immediately</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="1003"/>
        <location filename="../qml/SettingsTab.qml" line="1028"/>
        <source>Ngôn ngữ</source>
        <translation>Language</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="1011"/>
        <source>Ngôn ngữ hiển thị của giao diện — áp dụng ngay lập tức</source>
        <translation>The interface display language — applies immediately</translation>
    </message>
    <message>
        <location filename="../qml/SettingsTab.qml" line="1048"/>
        <source>Không thể lưu cài đặt</source>
        <translation>Could not save settings</translation>
    </message>
</context>
<context>
    <name>ShellBridge</name>
    <message>
        <location filename="../bridge.py" line="76"/>
        <source>Văn bản</source>
        <translation>Text</translation>
    </message>
    <message>
        <location filename="../bridge.py" line="77"/>
        <source>Đoạn văn</source>
        <translation>Paragraphs</translation>
    </message>
    <message>
        <location filename="../bridge.py" line="78"/>
        <source>Sách nói</source>
        <translation>Audiobooks</translation>
    </message>
    <message>
        <location filename="../bridge.py" line="79"/>
        <source>Sao chép giọng</source>
        <translation>Voice cloning</translation>
    </message>
    <message>
        <location filename="../bridge.py" line="80"/>
        <source>Cài đặt</source>
        <translation>Settings</translation>
    </message>
</context>
<context>
    <name>TextTab</name>
    <message>
        <location filename="../qml/TextTab.qml" line="84"/>
        <source>Xuất âm thanh WAV</source>
        <translation>Export WAV audio</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="127"/>
        <source>Studio Tổng hợp Văn bản</source>
        <translation>Text Synthesis Studio</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="128"/>
        <source>Nhập văn bản tiếng Việt hoặc Anh, gắn thẻ biểu cảm và trải nghiệm giọng đọc AI chất lượng cao.</source>
        <translation>Type Vietnamese or English text, tag emotions, and enjoy high-quality AI voices.</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="134"/>
        <source>Nội dung văn bản</source>
        <translation>Text content</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="135"/>
        <source>Hỗ trợ tiếng Việt đa vùng miền và tiếng Anh xen kẽ</source>
        <translation>Supports regional Vietnamese and mixed Vietnamese–English</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="152"/>
        <source>%1 từ · %2 ký tự · ~%3s</source>
        <translation>%1 words · %2 characters · ~%3s</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="164"/>
        <source>Xóa</source>
        <translation>Clear</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="189"/>
        <source>Nhập hoặc dán văn bản tiếng Việt / English…</source>
        <translation>Type or paste Vietnamese / English text…</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="222"/>
        <source>Biểu cảm</source>
        <translation>Emotions</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="227"/>
        <source>nhấn để chèn tại con trỏ: [cười] [thở dài] [hắng giọng]</source>
        <translation>click to insert at the cursor: [cười] [thở dài] [hắng giọng]</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="241"/>
        <source>Cười</source>
        <translation>Laugh</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="247"/>
        <source>Thở dài</source>
        <translation>Sigh</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="253"/>
        <source>Hắng giọng</source>
        <translation>Clear throat</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="276"/>
        <source>Giọng đọc &amp; Điều khiển</source>
        <translation>Voice &amp; Controls</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="288"/>
        <source>Giọng đọc:</source>
        <translation>Voice:</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="312"/>
        <source>Tạo âm thanh</source>
        <translation>Generate audio</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="316"/>
        <location filename="../qml/TextTab.qml" line="390"/>
        <source>Nhập văn bản để tạo âm thanh.</source>
        <translation>Enter text to generate audio.</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="317"/>
        <source>Tổng hợp phát trực tiếp (Ctrl+Return)</source>
        <translation>Synthesize and stream (Ctrl+Return)</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="329"/>
        <source>Phát</source>
        <translation>Play</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="329"/>
        <source>Dừng</source>
        <translation>Stop</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="334"/>
        <source>Tạo âm thanh trước khi phát.</source>
        <translation>Generate audio before playing.</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="337"/>
        <source>Dừng phát lại</source>
        <translation>Stop replay</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="338"/>
        <source>Phát lại âm thanh vừa tạo</source>
        <translation>Replay the generated audio</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="335"/>
        <source>Không phát hiện thiết bị âm thanh.</source>
        <translation>No audio device detected.</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="355"/>
        <source>Xuất WAV</source>
        <translation>Export WAV</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="358"/>
        <source>Tạo âm thanh trước khi xuất WAV.</source>
        <translation>Generate audio before exporting WAV.</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="359"/>
        <source>Chọn vị trí lưu tệp</source>
        <translation>Choose where to save the file</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="370"/>
        <source>Lưu nhanh</source>
        <translation>Quick save</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="375"/>
        <source>Tạo âm thanh trước khi lưu.</source>
        <translation>Generate audio before saving.</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="376"/>
        <source>Lưu vào thư mục xuất mặc định (Ctrl+E)</source>
        <translation>Save to the default export folder (Ctrl+E)</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="392"/>
        <source>Tạo âm thanh trước khi phát hoặc xuất.</source>
        <translation>Generate audio before playing or exporting.</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="433"/>
        <source>Đang tổng hợp…</source>
        <translation>Synthesizing…</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="494"/>
        <source>Hủy</source>
        <translation>Cancel</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="496"/>
        <source>Dừng tổng hợp (Esc)</source>
        <translation>Stop synthesis (Esc)</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="510"/>
        <source>Không thể tạo âm thanh</source>
        <translation>Could not generate audio</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="522"/>
        <location filename="../qml/TextTab.qml" line="537"/>
        <source>Đã hủy</source>
        <translation>Cancelled</translation>
    </message>
    <message>
        <location filename="../qml/TextTab.qml" line="543"/>
        <source>Đã xuất WAV</source>
        <translation>WAV exported</translation>
    </message>
</context>
<context>
    <name>VoicePicker</name>
    <message>
        <location filename="../qml/components/VoicePicker.qml" line="12"/>
        <source>Giọng đọc</source>
        <translation>Voice</translation>
    </message>
    <message>
        <location filename="../qml/components/VoicePicker.qml" line="13"/>
        <location filename="../qml/components/VoicePicker.qml" line="156"/>
        <source>Chọn giọng đọc</source>
        <translation>Choose a voice</translation>
    </message>
    <message>
        <location filename="../qml/components/VoicePicker.qml" line="254"/>
        <source>Tìm giọng đọc…</source>
        <translation>Search voices…</translation>
    </message>
</context>
</TS>
